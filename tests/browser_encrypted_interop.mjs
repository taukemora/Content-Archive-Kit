#!/usr/bin/env node
/*
 * Independent Web Crypto interoperability probe for the encrypted site.
 *
 * Node 26 exposes the same Web Crypto API the browser reader uses, so this
 * script re-derives the frozen protocol without importing any Python code.
 * It is invoked by tests/test_encrypted_site.py with a JSON probe file.
 */
import { webcrypto } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

const subtle = webcrypto.subtle;
const encoder = new TextEncoder();
const decoder = new TextDecoder('utf-8', { fatal: true });
const PROTOCOL = 'CA-ENC-1';
const OBJECT_RE = /^objects\/([0-9a-f]{64})\.([0-9a-f]{32})\.bin$/;

function fail(message) {
  throw new Error(message);
}

function lp(bytes) {
  const out = new Uint8Array(4 + bytes.length);
  new DataView(out.buffer).setUint32(0, bytes.length);
  out.set(bytes, 4);
  return out;
}

function aad(archiveId, purpose, oid) {
  const parts = [PROTOCOL, archiveId, purpose, oid].map((v) => encoder.encode(v));
  const total = parts.reduce((n, p) => n + 4 + p.length, 0);
  const out = new Uint8Array(total);
  let offset = 0;
  for (const part of parts) {
    const block = lp(part);
    out.set(block, offset);
    offset += block.length;
  }
  return out;
}

function b64(value, expect) {
  const raw = Buffer.from(value, 'base64');
  if (raw.length !== expect) fail('bad base64 length');
  return new Uint8Array(raw);
}

function hex(bytes) {
  return Buffer.from(bytes).toString('hex');
}

async function deriveKek(passphrase, salt, iterations) {
  const material = await subtle.importKey('raw', encoder.encode(passphrase), 'PBKDF2', false, ['deriveBits']);
  return subtle.deriveBits({ name: 'PBKDF2', salt, iterations, hash: 'SHA-256' }, material, 256);
}

async function unwrapDek(site, passphrase) {
  const kekBits = await deriveKek(passphrase, b64(site.kdf.salt, 16), site.kdf.iterations);
  const kek = await subtle.importKey('raw', kekBits, 'AES-GCM', false, ['decrypt']);
  const dek = await subtle.decrypt(
    {
      name: 'AES-GCM',
      iv: b64(site.wrap.nonce, 12),
      additionalData: aad(site.archive_id, 'wrap', 'dek'),
      tagLength: 128,
    },
    kek,
    b64(site.wrap.ciphertext, 48)
  );
  if (dek.byteLength !== 32) fail('bad dek');
  return new Uint8Array(dek);
}

async function decryptObject(file, dek, site, purpose, oid) {
  const blob = fs.readFileSync(file);
  if (blob.subarray(0, 4).toString('latin1') !== 'CAE1') fail('bad magic');
  const key = await subtle.importKey('raw', dek, 'AES-GCM', false, ['decrypt']);
  return new Uint8Array(
    await subtle.decrypt(
      {
        name: 'AES-GCM',
        iv: new Uint8Array(blob.subarray(4, 16)),
        additionalData: aad(site.archive_id, purpose, oid),
        tagLength: 128,
      },
      key,
      new Uint8Array(blob.subarray(16))
    )
  );
}

async function verifyIndexAddress(dek, oid) {
  const hkdf = await subtle.importKey('raw', dek, 'HKDF', false, ['deriveBits']);
  const bits = await subtle.deriveBits(
    { name: 'HKDF', hash: 'SHA-256', salt: new Uint8Array(0), info: encoder.encode('CA-ENC-1/address') },
    hkdf,
    256
  );
  const key = await subtle.importKey('raw', bits, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  const sig = await subtle.sign('HMAC', key, encoder.encode('CA-ENC-1/oid\u0000index\u0000index'));
  if (hex(new Uint8Array(sig)) !== oid) fail('index address mismatch');
}

async function expectReject(fn, label, flags) {
  try {
    await fn();
  } catch (e) {
    flags[label] = true;
    return;
  }
  flags[label] = false;
}

async function main() {
  const probe = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  const dir = probe.dir;
  const site = JSON.parse(fs.readFileSync(path.join(dir, 'site.json'), 'utf8'));
  const flags = {};

  const dek = await unwrapDek(site, probe.passphrase);
  const indexMatch = OBJECT_RE.exec(site.index.file);
  if (!indexMatch) fail('bad index path');
  await verifyIndexAddress(dek, indexMatch[1]);
  const indexPlain = await decryptObject(
    path.join(dir, site.index.file),
    dek,
    site,
    'index',
    indexMatch[1]
  );
  const index = JSON.parse(decoder.decode(indexPlain));
  if (index.format !== 'contentarchive-encrypted-index' || index.articles.length !== 2) {
    fail('unexpected index');
  }
  if (!JSON.stringify(index).includes(probe.needles[1])) fail('summary needle missing');

  let images = 0;
  for (const article of index.articles) {
    const match = OBJECT_RE.exec(article.body.file);
    if (!match || match[1] !== article.id) fail('body id mismatch');
    const body = decoder.decode(await decryptObject(path.join(dir, article.body.file), dek, site, 'article', match[1]));
    const img = /data-ca-src="(objects\/[^"]+)"/.exec(body);
    if (img) {
      const imgMatch = OBJECT_RE.exec(img[1]);
      const plain = await decryptObject(path.join(dir, img[1]), dek, site, 'image', imgMatch[1]);
      if (!Buffer.from(plain).includes(Buffer.from('ALPHAIMAGE'))) fail('image bytes mismatch');
      images += 1;
    }
  }
  const firstBody = decoder.decode(
    await decryptObject(
      path.join(dir, index.articles.find((a) => a.title === '加密标题甲').body.file),
      dek,
      site,
      'article',
      OBJECT_RE.exec(index.articles.find((a) => a.title === '加密标题甲').body.file)[1]
    )
  );
  if (!firstBody.includes(probe.needles[0])) fail('body needle missing');

  await expectReject(() => unwrapDek(site, probe.wrong), 'wrongPassphraseRejected', flags);

  const tamperedSite = JSON.parse(JSON.stringify(site));
  const wrapped = Buffer.from(tamperedSite.wrap.ciphertext, 'base64');
  wrapped[0] ^= 1;
  tamperedSite.wrap.ciphertext = wrapped.toString('base64');
  await expectReject(() => unwrapDek(tamperedSite, probe.passphrase), 'tamperedWrapRejected', flags);

  const articleRel = index.articles[0].body.file;
  const articleOid = OBJECT_RE.exec(articleRel)[1];
  const tampered = Buffer.from(fs.readFileSync(path.join(dir, articleRel)));
  tampered[tampered.length - 1] ^= 1;
  const tamperedPath = path.join(dir, 'tampered-probe.bin');
  fs.writeFileSync(tamperedPath, tampered);
  try {
    await expectReject(
      () => decryptObject(tamperedPath, dek, site, 'article', articleOid),
      'tamperedObjectRejected',
      flags
    );
  } finally {
    fs.unlinkSync(tamperedPath);
  }

  const ok =
    flags.wrongPassphraseRejected === true &&
    flags.tamperedWrapRejected === true &&
    flags.tamperedObjectRejected === true &&
    images >= 1;
  process.stdout.write(
    JSON.stringify({
      ok,
      articles: index.articles.length,
      images,
      ...flags,
    }) + '\n'
  );
  if (!ok) process.exit(1);
}

main().catch((error) => {
  process.stderr.write(String(error && error.stack ? error.stack : error) + '\n');
  process.exit(1);
});
