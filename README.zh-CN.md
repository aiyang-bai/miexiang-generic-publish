# 咩想工件发布器

[English](README.md)

给一个软件文件明确的产品、版本及渠道身份，发布后从最终消费者 URL 下载真实字节，核对大小和 SHA-256，再生成 `MIEXIANG_RELEASE_MANIFEST_V1` 清单。同一身份相同字节可重试；同一版本不同字节必须冲突。

**版本：0.2.0rc1，首个公开候选，采用 MIT 许可证。版权主体为 aiyang-bai。**

## 无账号安装与验证

声明 Python `>=3.11,<3.13`。本轮实际运行 Windows + Python 3.11；3.12 是待运行的 CI 目标。Linux/macOS 未实际运行，不作兼容承诺。

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install .
.venv/Scripts/miexiang-publish offline-demo
```

也可用 `python -m pip install --no-deps <wheel路径>` 安装审阅包，然后执行 `miexiang-publish offline-demo`。基础包没有 PyPI 运行依赖；真实 GitHub 发布另需 GitHub CLI。

离线 demo 只在内存中使用合成夹具验证身份、哈希、长度错误、冲突及清单时间变化。输出 `OFFLINE_FIXTURE_PASS`，远端验证为 `NOT_RUN`；不读取配置、不认证、不联网、不上传，不返回公网已发布清单。

## 目录与配置

```powershell
miexiang-publish --data-dir "./运行 数据" offline-demo
miexiang-publish --data-dir "./运行 数据" --profile ./profile.json auth-status
```

优先级为 `--data-dir`、`MIEXIANG_PUBLISH_DATA_DIR`、系统用户数据目录。Windows 默认为 `%LOCALAPPDATA%/miexiang-publish`；macOS 和 Linux 默认分支见英文说明，这不代表已在对应系统运行。数据目录内保存 `staging`、`locks`、`reports` 和默认 `profile.json`，绝不写入安装包目录。显式目录失败不会回退其他盘。库调用者可在进程开始时调用 `paths.configure_runtime(path)`；运行期间不支持其他线程切换全局目录。共同发布同一身份的进程须共用一个数据目录；不同机器或不同目录不共享本地锁。数据目录拒绝符号链接和 junction。

[GitHub 配置示例](examples/github.profile.json)默认禁用写入，只含合成仓库名。需复制到本地私有位置并明确填写自己授权的公开测试仓库；不要把示例当作可用资源。保留的 `cn-software-v1` 只是格式版本名，不保证国内可达。

## 真实发布

先单独安装 `gh` 并由本人交互登录系统凭据存储；不在 profile 写 token，不使用环境 token 或明文凭据后端。账号需要 release/tag 与仓库 contents 写权限。建议使用独立工件仓库或分支，清单会通过 contents API 提交到配置分支。

```powershell
miexiang-publish --profile ./profile.json preview --product-id example-tool --version 1.0.0 --channel test --artifact ./example.bin
miexiang-publish --profile ./profile.json publish --product-id example-tool --version 1.0.0 --channel test --artifact ./example.bin
```

`preview` 可联网只读检查，不能当成离线命令；`PREVIEW_READY` 不保证每个 provider 都准备好，需检查各项 readiness/identity。`publish` 只有配置明确授权才可写。上传命令成功不等于发布完成，工件和清单都须从消费者 URL 取回核对。本轮新 OSS 版本真实 GitHub 发布 `LIVE_E2E=NOT_RUN`；不以历史生产版本的 PASS 替代。

COS SDK 已拆为可选 extra：`python -m pip install '.[cos]'`；复现本轮可选依赖用 `requirements-cos.lock`。无 SDK 不影响 GitHub 导入和离线验证。COS 真实服务仍为 `IMPLEMENTED_UNVERIFIED`。不承担网站部署、云资源创建、代码签名、安装器行为、跨地区网络可达性或下载安全评估。

[Agent 状态与重试](docs/agent-usage.md)、[贡献检查](CONTRIBUTING.md)、[维护](MAINTENANCE.md)、[验证边界](docs/validation.md)、[来源](docs/provenance.md)、[MIT 许可证](LICENSE)。
