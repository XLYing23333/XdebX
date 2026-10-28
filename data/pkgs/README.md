# XdebX 精简安装缓存

默认保留启用且单文件不超过 5 MiB 的小包、插件、引导材料及必要的配套源码和许可证。
大包在 `manifest.json` 中标记 `bundle: on-demand`，版本、官方下载 URL、哈希与兼容记录仍保留，安装时按需获取。
`download_enabled=false` 仍表示退休或未使用资源，与按需下载不同。

CTypora 原始来源未知，旧包已移出默认缓存；需要使用时自行导入原包并显式接受未确认来源。
缓存包含运行所需的字体与音乐。

```sh
python3 tools/cache_packages.py          # 只补充默认小包
python3 tools/cache_packages.py --full   # 显式获取完整缓存与配套源码
python3 tools/cache_sources.py          # 只收集默认保留二进制的对应源码
python3 tools/build_release.py --repository XLYing23333/XdebX --output /tmp/xdebx-release
# 完整缓存构建时添加 --full-cache；输出目录须为空
cd data/pkgs
sha256sum -c SHA256SUMS
```

各发行版 deb 只适用于其声明的目标。缓存并非完整离线依赖仓库。
配套源码的 URL 和哈希保存在 `provenance/corresponding-source.json`，即使未随精简缓存附带也不移除来源记录。
