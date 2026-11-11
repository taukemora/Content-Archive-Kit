# Reader media prefetch acceptance / 阅读器图片预加载验收

## Status / 状态

**Local candidate accepted; not deployed. / 本地候选已验收，尚未部署。**

The candidate is based on `eb80703113d41e80a817c2fe5788400f180ef485` on
`fix/reader-media-prefetch`. The [source-bound receipt](reviews/2026-11-11-reader-media-prefetch.json)
records the six reviewed code/test hashes and parent-run results. Runtime author:
`deepseek-v4.1-flash` / `opencode-go`; independent reviewer:
`z-ai/glm-5.3-flash` / `commandcode`, verdict **PASS**. The parent
`gpt-6-astra` / `openai-codex` strengthened the normal-image fixture and ran the
acceptance independently. Subsequent documentation/inventory edits do not change
those six source bindings. This record does not authorize a commit, push, release,
private-instance upgrade, or publication.

The earlier [behavior acceptance](behavior-acceptance.md) and
[migration acceptance](acceptance.md) remain separate historical evidence; this
reader change does not rerun or supersede their real-model intake checks.

本次只改阅读器调度与对应测试，不改变归档协议、抓取流程、密钥、发布配置或文章数据。
这里的通过是本地合成环境验收，不是新候选的 GitHub CI 或线上更新证明。

## Implemented contract / 已实现的行为

- Use `.reader-pane` as the scrolling viewport: visible media first, then media
  within two pane-heights below, then within two pane-heights above. Re-rank
  pending jobs on scrolling, resizing, and layout changes.
- At most three fetch/decrypt jobs run concurrently. Decoding is asynchronous
  and is not an additional concurrency-limited stage. A distant object is not
  requested until an occurrence enters the scheduling window.
- A repeated ciphertext/MIME pair shares one fetch and Blob URL. Rank each DOM
  occurrence independently; never infer visibility from an enclosing rectangle
  spanning distant occurrences.
- A hidden or unmeasurable pane pauses new requests, without cancelling work
  already in flight. Showing it again resumes scheduling; switching articles,
  locking, and exit paths stop scheduling and discard stale completions.
- Keep a measurable placeholder until the native image `load` event. Normal
  acceptance requires decoded pixels, not just an assigned Blob URL. Decode
  failure has its own error-placeholder test.
- Preserve existing MIME/path/size/AAD checks, CSP, and Blob URL revocation.
  No new runtime dependency or user-facing configuration is introduced.

中文摘要：**可见优先、向下提前两屏、向上补两屏、最多三路抓取与解密**；隐藏阅读区不再
继续发起新下载，重复图片按实际可见位置调度，图片解码前保留占位，切文或锁定不允许迟到写回。
这不是整篇图片一次性下载，也不是快速跳转或慢网络下“绝不等待”的保证。

## Executed evidence / 实际验证

All fixtures are synthetic. The parent exported the baseline with Git's tracked
file modes, overlaid the exact six frozen candidate files, and installed both
requirements files in a fresh Python 3.13.5 environment with `PYTHONPATH` cleared.
`pip check` and the regenerated export's repository gate passed. Node was
26.12.1; real Chromium was 145.0.7632.6.

| Check | Actual result |
| --- | --- |
| Complete default unittest discovery | **737 total: 734 passed, 3 skipped; no failures/errors** |
| Scheduler module plus opt-in normal/decode-error/tamper browser test | **8/8 passed**, no skips |
| Node scheduler behavior cases | **15/15 passed**; these are nested cases, not extra unittest passes |
| Independent original-delay probe | Second image starts before the delayed first completes and decodes while still below the fold; distant images stay unrequested |
| Hidden mobile pane | Requests at hide **1**, after hide **1**, new late objects **0**; reopening works |
| Repeated image | A visible later occurrence loads even when its first occurrence is far above the pane |
| Assignment-to-decode interval | Three observed pending image boxes remain **405 px → 405 px**, not zero |
| Lifecycle/layout | Desktop, focus, 390×844 mobile (including orientation change), and 320×640 narrow viewport pass |
| Blob lifecycle | **3 created, 3 revoked, 0 active** after locking; delayed decrypt after lock/switch creates no extra Blob URL |
| Negative controls | Reintroducing hidden-pane scheduling or a spanning bounding box makes the corresponding Node test fail; parent replay confirmed both |
| Two-file incremental upgrade rehearsal | Only **`index.html`, `reader.js`, `site.json`** change; **0** objects re-encrypted, **4** reused; repeated build is a no-op and synthetic archive bytes are unchanged |

The default skips are one real-instance dry run and two opt-in browser tests.
The browser checks above exercise both skipped browser paths separately. These
counts overlap and must not be added together. The normal browser case now uses
a valid generated PNG and requires `complete` plus positive `naturalWidth`;
authenticated-but-undecodable bytes must produce an error in a separate case.
Accepting either successful pixels **or** an error placeholder would be a weaker
happy-path assertion and was explicitly rejected.

首版候选虽通过原有测试，但父代理真实浏览器探针发现隐藏 pane 后台继续下载、可见重复图漏载、
赋 `src` 后占位塌陷三项缺陷。修复后重跑原失败探针全部通过；没有用“原测试全绿”替代边界验收。

### Reproduce the repository test paths

```bash
# Run from the repository root, with declared dependencies installed.
node tests/reader_media_scheduler.mjs
cd tests
env -u PYTHONPATH ../.venv/bin/python -B -m unittest discover -s . -t .
# Set CA_PLAYWRIGHT_PYTHON to an interpreter with Playwright installed.
# Optionally set CA_PLAYWRIGHT_BROWSER to an existing Chromium executable.
env -u PYTHONPATH CA_RUN_BROWSER_TESTS=1 \
  CA_PLAYWRIGHT_PYTHON="$CA_PLAYWRIGHT_PYTHON" \
  ../.venv/bin/python -B -m unittest \
  test_reader_media_scheduler test_encrypted_site.TestBrowserAcceptance -v
```

See [test instructions](../tests/README.md) for the repository gate and optional
browser setup. The standalone parent probes are additional local evidence; the
public receipt is not a claim that every parent probe is part of hosted CI.

## Upgrade boundaries / 更新边界

1. **Upstream:** commit the coherent reviewed code/tests/docs unit only after
   approval; push/PR and exact-candidate hosted CI are subsequent gates. The
   existing workflow does not run Chromium. Merge approval is not deployment.
2. **Installed skill:** a separately approved, pinned minimal update of the
   conversational intake/README-before-CLI contract. Review the actual installed
   delta and preserve instance-specific rules; no whole-profile replacement.
   A skill update does not fix image scheduling.
3. **Private instance:** after upstream acceptance, review and back up only
   `scripts/encrypted_reader.js` and `scripts/archive_page.html`, install the
   pinned pair, and verify. Do not replace all scripts, `bin/ca`, entries,
   media, state, source settings, cron, or notification settings.
4. **Publication target:** after the instance upgrade is approved and verified,
   build and inspect before publishing through the existing approved process.
   With unchanged input/configuration, the rehearsal changed only the three
   public assets above. Concurrent content changes can legitimately produce a
   different delta and require inspection. Verify the exact deployed version;
   a push alone is not online acceptance. Rollback should restore the prior
   reader pair and rebuild through the publisher, not manually forge metadata
   or force-push history.

## Limits / 未验证范围

No private archive, installed skill, cron, NAS/container setting, or publication
target was modified. No candidate commit, hosted CI, tag, Release, or live-site
upgrade is established here. Localhost injected delays demonstrate scheduling,
not Internet/mobile-device latency guarantees; Safari/Firefox and browser heap
reclamation were not measured. The scheduling window does not guarantee that an
arbitrarily fast scroll will find every image already decoded.
