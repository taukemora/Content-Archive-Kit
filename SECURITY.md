# Security

## What this repository is

Content Archive Kit's public repository contains generic source code, the
canonical Hermes skill, configuration examples, synthetic tests, and
documentation. It must not contain private articles, `.state` runtime data,
credentials, personal recipients, or machine-local configuration; those live in
private archive instances outside this repository. The same exclusion applies
to any security report: **do not include private archive material, credentials,
or instance configuration in an issue or advisory.**

## Trust boundaries and known limits

- **Trusted code, not a sandbox.** The toolkit and its checks assume a trusted
  operator on a trusted machine. `tools/check_repository.py` is an integrity
  and privacy risk screen, not an untrusted-code sandbox: its example-loader
  step executes the repository's own Python, so a malicious tree could perform
  arbitrary actions despite the checker's read-only verification mode.
- **Checks are screens, not proof.** The public-tree manifest and private-
  material screens cannot prove that a tree is free of all private content or
  that its publisher is authentic; they do not inspect Git history, article
  semantics, or remote services.
- **Encrypted publication is not account authentication or DRM.** Content is
  encrypted locally with a random data-encryption key; a passphrase-derived
  wrapping key protects that key for browser unlock. Ciphertext remains
  publicly downloadable on a public host. This does not prevent offline
  passphrase guessing, copying after unlock, screenshots or key sharing, and
  cannot revoke an already downloaded ciphertext/key pair. A compromised
  hosted decryptor can capture secrets or plaintext when used. Encryption does
  not establish permission to republish copyrighted material.
- **Bounded skill evidence.** Probe and conversation harnesses measure
  authorization and routing decisions under specific, documented conditions
  (see [docs/behavior-acceptance.md](docs/behavior-acceptance.md)); they do not
  guarantee model behavior in every configuration, and the skill's written
  rules are not an enforcement mechanism.

## Reporting a vulnerability

- If GitHub private vulnerability reporting is enabled for this repository,
  please use it (Security → Report a vulnerability). That keeps details out of
  public view.
- This repository does not currently document an enabled private reporting
  channel. If private reporting is not available to you, open a **public
  issue** that states only that you have a security concern and asks the
  maintainer to arrange a private channel. Include **no vulnerability details,
  affected files, exploit steps, or sample payloads** in that public issue;
  hold them for the private conversation.
- Do not include private archive articles, `.state` data, credentials,
  recipients, or machine-local configuration in any report, public or private.
- No response-time commitment is offered, and this document does not change any
  repository setting; enabling or configuring reporting channels is a
  maintainer decision made in repository settings, not here.

## Scope

There is no released version or maintained release-support matrix yet; report
the affected commit and Linux environment. Report defects in the toolkit's
code or shipped guidance here, including defects exposed by an instance or
publication adapter. Third-party service administration and instance-specific
misconfiguration are not maintained by this repository. Deployment guidance is
in [skills/content-archive/references/deployment.md](skills/content-archive/references/deployment.md).

## 中文摘要

仓库及安全报告不得包含私人实例材料。完整性检查不是不可信代码沙箱，也不是隐私或真实性的绝对证明。发布时在本地加密，口令派生的包装密钥保护随机数据密钥，浏览器解锁后解密；这不是账户认证或 DRM，不防离线口令猜测、解锁后复制、密钥转发或被篡改的托管解密程序，也不能撤销已下载的密文与密钥，更不代表取得转载许可。技能规则不保证所有模型配置都遵守。若 GitHub 私密漏洞报告可用则使用；否则公开 issue 只请求安排私密渠道，不附漏洞细节或载荷。不承诺响应时限，不更改仓库设置；请报告受影响提交，工具包代码及所附指南的缺陷仍在报告范围内。
