#!/usr/bin/env python3
"""
用 Playwright 浏览器自动化下载候选池ETF历史数据（绕过东方财富反爬）

只下载候选池22只ETF，用于ETF轮动策略回测。
"""

import json
import time
import pandas as pd
from pathlib import Path
from playwright.sync_api import sync_playwright

# ============ 配置 ============

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "raw" / "etf" / "daily"
LOG_FILE = PROJECT_ROOT / "data" / "_workspace" / "download.log"

# 候选池32只ETF（code格式：sh.510300）
CANDIDATE_POOL = {
    # 宽基6只
    "sh.510300": "沪深300ETF",
    "sh.510500": "中证500ETF",
    "sz.159915": "创业板ETF",
    "sh.510050": "上证50ETF",
    "sh.588000": "科创50ETF",
    "sh.512100": "中证1000ETF",
    # 行业19只
    "sh.512010": "医药ETF",
    "sz.159928": "消费ETF",
    "sh.515000": "科技ETF",
    "sh.516160": "新能源ETF",
    "sh.512000": "券商ETF",
    "sh.512800": "银行ETF",
    "sh.512660": "军工ETF",
    "sh.512480": "半导体ETF",
    "sh.515880": "通信ETF",
    "sh.512200": "房地产ETF",
    "sh.512980": "传媒ETF",
    "sh.512400": "有色金属ETF",
    "sh.515220": "煤炭ETF",
    "sh.515210": "钢铁ETF",
    "sz.159870": "化工ETF",
    "sh.516950": "基建ETF",
    "sz.159825": "农业ETF",
    "sh.512580": "环保ETF",
    # 跨境4只
    "sh.513100": "纳指ETF",
    "sh.513500": "标普500ETF",
    "sh.513130": "恒生科技ETF",
    "sz.159920": "恒生ETF",
    # 防御3只
    "sh.511010": "国债ETF",
    "sh.511990": "货币ETF",
    "sh.518880": "黄金ETF",
}


def log(msg):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] [ETF-PW] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def get_secid(code):
    """code格式 sh.510300 → 东方财富secid 1.510300（沪市1，深市0）"""
    exchange, symbol = code.split(".")
    prefix = "1" if exchange == "sh" else "0"
    return f"{prefix}.{symbol}"


def download_etf_with_playwright(page, code, name):
    """用playwright下载单只ETF历史数据"""
    secid = get_secid(code)

    # 东方财富K线API
    url = (
        f"https://push2his.eastmoney.com/api/qt/stock/kline/get?"
        f"fields1=f1,f2,f3,f4,f5,f6&"
        f"fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61&"
        f"beg=20000101&end=20261231&"
        f"rtntype=6&secid={secid}&klt=101&fqt=1"
    )

    try:
        # 用page.evaluate调用fetch，这样会携带浏览器的请求头和Cookie
        result = page.evaluate(f"""
        async () => {{
            const response = await fetch('{url}', {{
                method: 'GET',
                headers: {{
                    'User-Agent': navigator.userAgent,
                    'Referer': 'https://quote.eastmoney.com/',
                }},
                credentials: 'include'
            }});
            const data = await response.json();
            return data;
        }}
        """)

        if result.get("data") and result["data"].get("klines"):
            klines = result["data"]["klines"]
            # 解析K线数据：日期,开盘,收盘,最高,最低,成交量,成交额,振幅,涨跌幅,涨跌额,换手率
            data_list = []
            for line in klines:
                parts = line.split(",")
                if len(parts) >= 11:
                    data_list.append({
                        "date": parts[0],
                        "open": float(parts[1]),
                        "close": float(parts[2]),
                        "high": float(parts[3]),
                        "low": float(parts[4]),
                        "volume": float(parts[5]),
                        "amount": float(parts[6]),
                        "amplitude": float(parts[7]),
                        "pctChg": float(parts[8]),
                        "change": float(parts[9]),
                        "turn": float(parts[10]),
                        "code": code,
                    })

            df = pd.DataFrame(data_list)
            df = df.sort_values("date").reset_index(drop=True)
            return df
        else:
            log(f"⚠️ {code} {name} 无数据：{str(result)[:200]}")
            return None

    except Exception as e:
        log(f"❌ {code} {name} 下载失败：{e}")
        return None


def save_etf_data(code, df):
    exchange = code.split(".")[0]
    filepath = DATA_DIR / exchange / f"{code}.csv"
    filepath.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(filepath, index=False, encoding="utf-8")
    return filepath


def main():
    log("=" * 60)
    log("开始用Playwright下载候选池ETF历史数据（东方财富，前复权）")
    log(f"候选池：{len(CANDIDATE_POOL)}只ETF")
    log("=" * 60)

    with sync_playwright() as p:
        # 启动浏览器（用系统已有的Chrome）
        browser = p.chromium.launch(headless=True, channel="chrome")
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        page = context.new_page()

        # 先访问东方财富主页，获取Cookie
        log("访问东方财富主页，获取Cookie...")
        page.goto("https://quote.eastmoney.com/", wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)

        success_count = 0
        fail_count = 0

        for idx, (code, name) in enumerate(CANDIDATE_POOL.items(), 1):
            log(f"[{idx}/{len(CANDIDATE_POOL)}] 下载 {code} {name} ...")

            df = download_etf_with_playwright(page, code, name)

            if df is not None and len(df) > 0:
                filepath = save_etf_data(code, df)
                log(f"[{idx}/{len(CANDIDATE_POOL)}] ✅ {code} {name} 完成：{len(df)}条（{df['date'].min()} ~ {df['date'].max()}）")
                success_count += 1
            else:
                fail_count += 1

            # 间隔，避免请求太快
            time.sleep(2)

        browser.close()

    log("=" * 60)
    log(f"下载完成：成功 {success_count}，失败 {fail_count}")
    log("=" * 60)


if __name__ == "__main__":
    main()
