# 常见问题排查

## 雪球 / Xueqiu: API 返回 400

**症状：** `agent-reach doctor` 显示雪球 ⚠️，报 `HTTP Error 400`

**原因：** 雪球 API 需要登录 Cookie，无法通过匿名访问获取。

**解决方案：** 在 Chrome 里登录 xueqiu.com，然后运行：

```bash
agent-reach configure --from-browser chrome --platform xueqiu
```

再次运行 `agent-reach doctor` 确认恢复 ✅。Cookie 过期后重新运行即可。

---

## YouTube: unable to extract yt initial data

**症状：** `agent-reach transcribe <YouTube URL>` 或 `yt-dlp` 报

```
WARNING: [youtube] unable to extract yt initial data
WARNING: [youtube] Incomplete data received in embedded initial data
```

**原因：** yt-dlp 版本过旧。YouTube 每隔几周就会改动页面结构，
跟上这些改动正是 yt-dlp 的核心价值——所以一个「能跑」的旧版本依然会在
真正提取时失败。注意 `yt-dlp --version` 正常、`agent-reach doctor` 也曾
报告可用，都不能证明提取器还有效。

**解决方案：**

```bash
python -m pip install -U "yt-dlp[default]"
```

> **`yt-dlp -U` 对 pip 安装无效**，它会直接拒绝：
> `You installed yt-dlp with pip or using the wheel from PyPi; Use that to update`。
> 必须用上面的 pip 命令。

`[default]` extra 很重要：它会带上 `yt-dlp-ejs`，用于解 YouTube 的 JS 挑战。

自 v1.5.1 起 `agent-reach doctor` 会在 yt-dlp 发布超过 42 天时主动降级为
⚠️ 并给出这条升级命令，不再把陈旧版本报成 ✅。

---

## 所有 API 调用失败：certificate verify failed / self signed certificate in certificate chain

**症状：** `agent-reach transcribe` 或其他联网命令报

```
SSLError(SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED]
certificate verify failed: self signed certificate in certificate chain'))
```

浏览器访问同一个网站却完全正常。

**原因：** 本机 TLS 被中间人拦截——通常是杀毒软件的「加密连接扫描」
（Kaspersky、Avast、ESET、Bitdefender 都有这个功能），也可能是企业代理
（Zscaler 等）或调试代理（Fiddler/Charles）。拦截方用自己的根证书重新签发
证书；该根证书装在**操作系统信任库**里，所以浏览器信任它，但 Python 的
`requests` 只信任 **certifi** 自带的根证书列表，于是校验失败。

这类拦截常常只针对部分域名，所以你可能看到「yt-dlp 下载正常，但调用
Whisper API 失败」这种不对称现象。

**先确认是谁在拦截：**

```bash
echo | openssl s_client -connect api.groq.com:443 -servername api.groq.com 2>/dev/null | grep "^ *1 s:"
```

如果输出里出现杀毒软件或代理厂商的名字（例如
`O=AO Kaspersky Lab, CN=Kaspersky Anti-Virus Personal Root Certificate`），
就确认了。

**三种解决方案——都不要关闭证书校验。** 用 `verify=False` 或
`PYTHONHTTPSVERIFY=0` 会让所有请求失去中间人防护，而这里的中间人本来就在。

### 方案 1：把拦截方根证书合并进 certifi 副本（推荐）

不要直接改 certifi 自带的 `cacert.pem`——升级 certifi 会覆盖它。

Windows（PowerShell）：

```powershell
# 1. 找到拦截方根证书的指纹
Get-ChildItem Cert:\LocalMachine\Root | Where-Object { $_.Subject -like "*Kaspersky*" } |
  Select-Object Subject, Thumbprint

# 2. 导出为 PEM
$cert = Get-ChildItem Cert:\LocalMachine\Root | Where-Object { $_.Thumbprint -eq "<上一步的指纹>" }
$b64  = [Convert]::ToBase64String($cert.RawData, 'InsertLineBreaks')
Set-Content "$env:TEMP\interceptor.pem" "-----BEGIN CERTIFICATE-----`n$b64`n-----END CERTIFICATE-----" -Encoding ascii

# 3. 合并 certifi + 拦截方根证书
$certifi = python -c "import certifi; print(certifi.where())"
$bundle  = "$env:USERPROFILE\.agent-reach\ca-bundle.pem"
Get-Content $certifi, "$env:TEMP\interceptor.pem" | Set-Content $bundle -Encoding ascii

# 4. 持久指向合并后的 bundle
[Environment]::SetEnvironmentVariable('SSL_CERT_FILE',      $bundle, 'User')
[Environment]::SetEnvironmentVariable('REQUESTS_CA_BUNDLE', $bundle, 'User')
```

设置完必须**重开终端**才生效；VS Code 等已在运行的程序要整个重启，
因为集成终端继承的是宿主进程的环境。

> 这个 bundle 是 certifi 的快照。以后升级 certifi 时，bundle 不会自动跟着更新——
> 如果某天公共网站开始报证书错误，重新执行第 3 步即可。

macOS / Linux：

```bash
python - <<'EOF'
import certifi, pathlib
bundle = pathlib.Path.home() / ".agent-reach" / "ca-bundle.pem"
bundle.parent.mkdir(parents=True, exist_ok=True)
bundle.write_text(
    pathlib.Path(certifi.where()).read_text()
    + "
"
    + pathlib.Path("interceptor.pem").read_text()
)
print(bundle)
EOF
export SSL_CERT_FILE="$HOME/.agent-reach/ca-bundle.pem"
export REQUESTS_CA_BUNDLE="$SSL_CERT_FILE"
```

### 方案 2：truststore（不用维护 bundle）

让 Python 直接读操作系统信任库——拦截方根证书本来就在里面：

```bash
pip install truststore
```

然后在进程启动时调用 `truststore.inject_into_ssl()`。`pip` 自己就是这么解决
同一个问题的。好处是 certifi 升级后不会失效。

### 方案 3：在杀毒软件里排除该域名

在杀毒软件设置里关闭对应域名的加密连接扫描
（Kaspersky：设置 → 网络 → 加密连接扫描 → 排除列表）。
一次生效、对所有工具都管用，也不用动 Python，但只覆盖你手动加进去的域名。

---

## Windows: UnicodeEncodeError / charmap codec can't encode character

**症状：** 直接调用上游工具（`bili`、`twitter`、`opencli` 等）时报

```
UnicodeEncodeError: 'charmap' codec can't encode character '用'
```

**原因：** Windows 控制台默认用 cp1252/GBK 代码页，遇到中文输出就崩。
`agent-reach` 自己的命令不受影响（CLI 启动时会把 stdout 包成 UTF-8），
但 SKILL.md 的设计是让 Agent **直接调用上游工具**，那些进程没有这层保护。

**解决方案：** 在调用前设置 UTF-8：

```bash
# Git Bash / WSL
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8
```

```powershell
# PowerShell
$env:PYTHONUTF8 = "1"; $env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [Text.Encoding]::UTF8
```

想永久生效就用 `[Environment]::SetEnvironmentVariable('PYTHONUTF8','1','User')`。

---

## Twitter/X: twitter-cli 连接失败

**症状：** `twitter search` 或其他命令返回错误

**原因：** twitter-cli 需要 `TWITTER_AUTH_TOKEN` 和 `TWITTER_CT0`
环境变量才能访问 Twitter API。`agent-reach configure twitter-cookies`
保存的值只供 doctor 检查配置是否齐全；doctor 不执行上游认证，也不会设置当前
Shell。如果你的网络环境需要代理才能访问 x.com，还需要配置代理。

**解决方案：**

### 方案 1：设置环境变量代理

```bash
export TWITTER_AUTH_TOKEN="..."
export TWITTER_CT0="..."
export HTTP_PROXY="http://user:pass@host:port"
export HTTPS_PROXY="http://user:pass@host:port"
twitter search "test" -n 1
```

### 方案 2：使用全局代理工具

让代理工具接管所有网络流量，这样 twitter-cli 的请求也会走代理：

```bash
# macOS — ClashX / Surge 开启"增强模式"
# Linux — proxychains 或 tun2socks
proxychains twitter search "test" -n 1
```

### 方案 3：不用 twitter-cli，用 Exa 搜索替代

twitter-cli 不可用时，可以直接用 Exa 搜索 Twitter 内容：

```bash
mcporter call exa.web_search_exa query="site:x.com 搜索词" numResults=5
```

### 方案 4：检查认证

```bash
twitter check
```

> 如果返回 "Missing credentials"，需要在运行该命令的进程环境中设置
> `TWITTER_AUTH_TOKEN` 和 `TWITTER_CT0`。
>
> **Fallback：** 如果你已经安装了 bird CLI（`npm install -g @steipete/bird`），它也能正常工作。Agent Reach 会自动检测已安装的工具。
