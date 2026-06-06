# 从被封到极速：一次爬虫对抗实录

> 目标：把 pesdb.net/efootball 上全部 37,650 张球员卡片的数据爬下来。  
> 最终结果：请求数从 38,827 次压缩到 1,177 次，耗时从 20 小时缩短到 30 分钟。

---

## 第一阶段：基础版上线，立刻遭限流

第一版爬虫逻辑很简单：

1. 爬 1,177 个列表页，收集所有球员 ID
2. 逐个请求 37,650 个球员详情页
3. 解析 HTML，存入 CSV

跑起来没多久，大量 HTTP 429 涌来。加了限速、随机延迟，还是反复触发。

---

## 问题一：Python 的 TLS 指纹出卖了你

`requests` 库发出的 HTTPS 请求，在 TLS 握手阶段就会暴露身份。

每个 TLS 客户端发起连接时，都会发送一个 ClientHello 报文，里面包含：

- 支持的加密套件列表（Cipher Suites）
- TLS 扩展的顺序和参数
- HTTP/2 的 SETTINGS 帧格式

这些特征组合在一起就是 **JA3/JA4 指纹**。Python 的 `urllib3` 生成的指纹和 Chrome/Firefox 完全不同，服务器在网络层就能认出你是爬虫——与 User-Agent 无关。

**解法：`curl_cffi`**

```python
# 之前
import requests
r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 ..."})

# 之后
from curl_cffi.requests import Session
s = Session(impersonate="chrome136")  # 完整复现 Chrome 的 TLS 握手
r = s.get(url)
```

`curl_cffi` 底层使用 libcurl，完整复现目标浏览器的：
- Cipher Suite 顺序
- TLS 扩展（ALPN、SNI、session ticket 等）
- HTTP/2 帧格式

---

## 问题二：impersonate 版本号填错直接报错

```
ImpersonateError: Impersonating firefox117 is not supported
```

`curl_cffi` 只支持特定版本号，不是所有版本都有。解决方法是先查询当前安装版本支持哪些目标：

```python
from curl_cffi.requests import BrowserType
print([b.value for b in BrowserType])
# ['chrome99', 'chrome116', 'chrome124', 'chrome136', 'firefox133', 'firefox135', ...]
```

从返回的列表里选，不要猜。

---

## 问题三：多线程下 curl_cffi 连接全部失败

换成 `curl_cffi` 之后，单线程测试正常，但多线程版本全是 `CONN ERR`。

原因：用模块级的 `curl_requests.get()` 在多个线程里并发调用，libcurl 的内部句柄初始化会冲突。

**解法：每个线程维护独立的 `Session` 对象**

```python
import threading
_tls = threading.local()

def _get_session(impersonate: str) -> Session:
    if not hasattr(_tls, "session") or _tls.impersonate != impersonate:
        _tls.session    = Session(impersonate=impersonate)
        _tls.impersonate = impersonate
    return _tls.session
```

`threading.local()` 保证每个线程拿到自己的 Session，互不干扰。

---

## 问题四：429 依然高频，换身份也没用

TLS 指纹解决了之后，429 还是不断触发。根本原因：**IP 级别的速率限制**。

服务器对同一 IP 在时间窗口内的请求数有上限，管你伪装成哪个浏览器都一样。

为了优雅地处理这个问题，实现了一套三层防护：

**1. 三层柏林噪声限速**  
用 Perlin 噪声模拟人类不均匀的点击节奏，而不是固定间隔：

```python
n1 = noise.pnoise1(t * 0.06) * 0.25  # 慢波：整体节奏漂移 ±25%
n2 = noise.pnoise1(t * 0.35) * 0.15  # 中波：短时爆发/暂停 ±15%
n3 = noise.pnoise1(t * 2.00) * 0.08  # 快波：单次请求抖动 ±8%
interval = base * (1 + n1 + n2 + n3)
```

每隔 35–90 个请求，还会随机插入 1.5–5 秒的"思考停顿"。

**2. ThrottleGate 全局熔断器**  
任何一个线程触发 429，立刻关闭全局闸门，所有线程暂停：

```python
COOLDOWNS = [20, 40, 60, 90, 120]  # 冷却时间随触发次数递增
```

冷却结束后自动切换新的浏览器 profile，换一个身份继续。

**3. 自适应退避**  
触发 429 → 将请求间隔乘以 1.6；每成功 100 次 → 将间隔乘以 0.95，逐步恢复正常速度。

---

## 转折点：发现根本不需要爬详情页

在分析网站结构时注意到一件事：页面的列显示是由 Cookie 控制的，Cookie 名就叫 `columns`。

浏览器开发者工具里找到提交列选择的 JS 函数：

```javascript
function submitColumns() {
    // 读取所有 id 以 col_ 开头的 checkbox
    // 拼成 ?columns=id,pos,name,speed,...
    document.location.search = Vars;
}
```

这意味着：**列表页支持直接显示所有属性字段**，包括速度、射门、传球等 118 列。

只需要在请求时携带正确的 Cookie：

```python
import urllib.parse
session.cookies.set(
    "columns",
    urllib.parse.quote("id,pos,name,overall_rating,speed,acceleration,...", safe=""),
    domain="pesdb.net",
)
```

验证结果：

```
Status: 200
Columns: 118, Data rows: 32
First player:
  ID: 8554076, Name: Safi Belal, Speed: 95, Overall: 116 ...
```

---

## 最终架构对比

|  | 原方案 | 优化后 |
|---|---|---|
| Phase 1（收集 ID） | 1,177 次请求 | 不需要 |
| Phase 2（详情页） | 37,650 次请求 | 不需要 |
| 列表页（含全列） | — | 1,177 次请求 |
| **总请求数** | **38,827** | **1,177** |
| 预计耗时 | 10–20 小时 | 30–60 分钟 |
| 触发限流风险 | 极高 | 低 |

---

## 核心经验

1. **先看网络请求，再写爬虫。** 很多网站的数据比 HTML 页面更早暴露在 XHR 或 Cookie 里，找对了入口可以省掉 97% 的工作量。

2. **TLS 指纹比 User-Agent 重要。** 服务器越来越多地在握手阶段就做识别，换 UA 没用，得换整个 TLS 栈。

3. **429 不一定是速度问题，可能是 IP 问题。** 再慢也没用，换 IP 才是根本。

4. **多线程 + HTTP 客户端，一定要看线程安全文档。** curl_cffi 的 Session 不能跨线程共享。

5. **全局熔断器比单请求退避更有效。** 发现限流时，让所有线程同时停下来比各自乱退避要稳定得多。

---

*工具栈：Python 3.12 · curl_cffi · BeautifulSoup · Rich · noise*
