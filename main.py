import os
import traceback
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, Depends, Query, Body
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from pysdk.grvt_ccxt import GrvtCcxt
from pysdk.grvt_ccxt_env import GrvtEnv
from pysdk.grvt_ccxt_logging_selector import logger
from pysdk.grvt_ccxt_types import GrvtOrderSide
from pysdk.grvt_ccxt_utils import rand_uint32

# 載入環境變數
load_dotenv()

# --- Pydantic Models (定義請求與回應資料結構) ---

class OrderRequest(BaseModel):
    symbol: str = Field(default="BTC_USDT_Perp", description="交易對")
    side: str = Field(..., pattern="^(buy|sell)$", description="買或賣")
    amount: float = Field(..., gt=0, description="數量")
    price: Optional[float] = Field(None, gt=0, description="價格 (限價單必填)")
    order_type: str = Field(default="limit", pattern="^(limit|market)$")

class CancelOrderRequest(BaseModel):
    order_id: Optional[str] = None
    client_order_id: Optional[int] = None

# --- Dependency Injection (依賴注入設定) ---

# --- 全域變數 ---
read_client: Optional[GrvtCcxt] = None   # 負責：行情、歷史、餘額
trade_client: Optional[GrvtCcxt] = None  # 負責：下單、刪單

@asynccontextmanager
async def lifespan(app: FastAPI):
    global read_client, trade_client
    try:
        env = GrvtEnv(os.getenv("GRVT_ENV", "testnet"))
        acc_id = os.getenv("GRVT_TRADING_ACCOUNT_ID")
        api_private_key = os.getenv("GRVT_PRIVATE_KEY")

        # 1. 初始化【查看專用】Client (使用 GRVT_PRIVATE_KEY)
        logger.info("Initializing READ-ONLY Client...")
        read_params = {
            "api_key": os.getenv("GRVT_API_KEY"),
            "trading_account_id": os.getenv("GRVT_TRADING_ACCOUNT_ID"),
            "private_key": api_private_key, # 查看用私鑰
        }
        read_client = GrvtCcxt(env, logger, parameters=read_params, order_book_ccxt_format=True)

        # 2. 初始化【交易專用】Client (使用 GRVT_PRIVATE_TRADE_KEY)
        logger.info("Initializing TRADING Client...")
        trade_params = {
            "api_key": os.getenv("GRVT_API_TRADE_KEY"),
            "trading_account_id": os.getenv("GRVT_TRADING_ACCOUNT_TRADE_ID"),
            "private_key": api_private_key, # 交易用私鑰
        }
        trade_client = GrvtCcxt(env, logger, parameters=trade_params, order_book_ccxt_format=True)
        
        yield
    except Exception as e:
        logger.error(f"Failed to initialize clients: {e}")
        raise e
    finally:
        logger.info("Shutting down...")

def get_read_api() -> GrvtCcxt:
    """取得查看權限的 API Client"""
    if read_client is None:
        raise HTTPException(status_code=503, detail="Read Client not initialized")
    return read_client

def get_trade_api() -> GrvtCcxt:
    """取得交易權限的 API Client"""
    if trade_client is None:
        raise HTTPException(status_code=503, detail="Trade Client not initialized")
    return trade_client

# --- FastAPI App ---

app = FastAPI(
    title="GRVT Trading API",
    description="Converted from test script to FastAPI",
    version="1.0.0",
    lifespan=lifespan
)

# --- Routes (API 路由) ---

@app.get("/")
def health_check():
    return {"status": "ok", "service": "GRVT API Proxy"}

# -------- Market Data --------


@app.get("/markets")
def get_markets(api: GrvtCcxt = Depends(get_read_api)):
    """獲取所有市場資訊"""
    try:
        return api.fetch_all_markets()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/instruments/{symbol}/ticker")
def get_ticker(symbol: str, api: GrvtCcxt = Depends(get_read_api)):
    """獲取特定交易對的 Ticker"""
    try:
        return api.fetch_ticker(symbol)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/instruments/{symbol}/orderbook")
def get_order_book(symbol: str, limit: int = 10, api: GrvtCcxt = Depends(get_read_api)):
    """獲取訂單簿"""
    try:
        return api.fetch_order_book(symbol, limit=limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# -------- Account --------

@app.get("/account/balance")
def get_balance(api: GrvtCcxt = Depends(get_read_api)):
    """
    獲取錢包餘額 (Assets)
    回傳內容包含：free (可用), used (凍結/佔用), total (總額)
    """
    try:
        return api.fetch_balance()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@app.get("/account/summary")
def get_account_summary(type: str = "sub-account", api: GrvtCcxt = Depends(get_read_api)):
    """獲取帳戶摘要"""
    try:
        return api.get_account_summary(type=type)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/account/positions")
def get_positions(symbols: Optional[List[str]] = Query(None), api: GrvtCcxt = Depends(get_read_api)):
    """獲取倉位資訊"""
    try:
        target_symbols = symbols if symbols else ["BTC_USDT_Perp"]
        return api.fetch_positions(symbols=target_symbols)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# -------- Trading (Orders) --------

@app.post("/orders")
def create_order(order: OrderRequest, api: GrvtCcxt = Depends(get_read_api)):
    """建立訂單 (買/賣)"""
    try:
        client_order_id = rand_uint32()
        
        params = {"client_order_id": client_order_id}
        
        # 根據訂單類型處理參數
        if order.order_type == "market":
            response = api.create_order(
                symbol=order.symbol,
                order_type="market",
                side=order.side, # type: ignore
                amount=Decimal(str(order.amount)),
                params=params
            )
        else:
            if not order.price:
                raise HTTPException(status_code=400, detail="Limit order requires price")
            
            response = api.create_order(
                symbol=order.symbol,
                order_type="limit",
                side=order.side, # type: ignore
                amount=order.amount,
                price=order.price,
                params=params
            )
            
        logger.info(f"Order created: {client_order_id=}, {response=}")
        return {"client_order_id": client_order_id, "response": response}

    except Exception as e:
        logger.error(f"Order creation failed: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/orders/open")
def get_open_orders_endpoint(symbol: str = "BTC_USDT_Perp", api: GrvtCcxt = Depends(get_read_api)):
    """查詢未成交訂單"""
    try:
        return api.fetch_open_orders(
            symbol=symbol,
            params={"kind": "PERPETUAL"},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/orders")
def cancel_order_endpoint(request: CancelOrderRequest, api: GrvtCcxt = Depends(get_read_api)):
    """取消單一訂單 (By ID or Client ID)"""
    try:
        if request.order_id:
            success = api.cancel_order(id=request.order_id, params={"time_to_live_ms": "1000"})
            return {"status": "success", "result": success}
        
        elif request.client_order_id:
             # CCXT 通常需要 order_id，如果 SDK 支援 client_order_id 取消則需修改此處
             # 這裡假設需要先查詢出 ID 才能取消，或是 SDK 支援 params 傳入
            success = api.cancel_order(id=None, params={"client_order_id": request.client_order_id, "time_to_live_ms": "1000"})
            return {"status": "success", "result": success}
        
        else:
            raise HTTPException(status_code=400, detail="Must provide order_id or client_order_id")
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/orders/all")
def cancel_all_orders_endpoint(api: GrvtCcxt = Depends(get_read_api)):
    """取消所有訂單"""
    try:
        response = api.cancel_all_orders()
        return {"status": "success", "response": response}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# -------- Risk Management --------

@app.get("/risk/derisk-ratio")
def get_derisk_ratio(api: GrvtCcxt = Depends(get_read_api)):
    """查看風險比率"""
    try:
        acc_summary = api.get_account_summary(type="sub-account")
        return {
            "maintenance_margin": acc_summary.get("maintenance_margin"),
            "derisk_margin": acc_summary.get("derisk_margin"),
            "derisk_ratio": acc_summary.get("derisk_to_maintenance_margin_ratio")
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/risk/derisk-ratio")
def set_derisk_ratio(ratio: str = Body(..., embed=True), api: GrvtCcxt = Depends(get_read_api)):
    """設定風險比率"""
    try:
        api.set_derisk_mm_ratio(ratio)
        return {"status": "success", "new_ratio": ratio}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@app.get("/exchange/info")
def get_exchange_description(api: GrvtCcxt = Depends(get_read_api)):
    """
    獲取交易所的完整描述 (原 print_description)
    包含：API 限制、支援的時間框架、交易對規則等靜態資訊。
    """
    try:
        # CCXT 的 describe() 會回傳一個非常巨大的 Dictionary
        return api.describe()
    except Exception as e:
        logger.error(f"Failed to fetch description: {e}")
        raise HTTPException(status_code=500, detail=str(e))
# -------- History (歷史紀錄) --------

@app.get("/history/orders")
def get_order_history(
    # 我們雖然在 API 介面上保留 symbol 讓前端傳，但 SDK 不支援直接傳入
    symbol: Optional[str] = Query(None, description="交易對 (部分 API 可能不支援篩選)"),
    limit: int = Query(10, le=100),
    api: GrvtCcxt = Depends(get_trade_api)
):
    """
    獲取歷史訂單 (Order History)
    修正: 移除 symbol 關鍵字參數，因為 SDK 不支援
    """
    try:
        # 準備參數
        request_params = {"kind": "PERPETUAL", "limit": limit}
        
        # 如果您確定 GRVT 的 API 支援透過 params 篩選 symbol，可以取消下面這行的註解：
        # if symbol:
        #     request_params["instrument"] = symbol  # 或是 "symbol": symbol，視 API 文件而定

        # 修正重點：只傳入 params，不傳入 symbol
        return api.fetch_order_history(params=request_params)

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/history/trades")
def get_my_trades(
    symbol: str = Query("BTC_USDT_Perp", description="交易對"),
    limit: int = Query(10, le=100),
    api: GrvtCcxt = Depends(get_trade_api)
):
    """
    獲取我的成交紀錄 (My Trades)
    對應原腳本: fetch_my_trades
    """
    try:
        return api.fetch_my_trades(
            symbol=symbol,
            limit=limit,
            params={}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/history/funding")
def get_funding_history(
    symbol: str = Query("BTC_USDT_Perp", description="交易對"),
    limit: int = Query(100, le=500),
    start_time: Optional[int] = Query(None, description="開始時間戳 (毫秒)"),
    api: GrvtCcxt = Depends(get_trade_api)
):
    """
    獲取資金費率歷史 (Funding Rate History)
    對應原腳本: fetch_funding_history
    """
    try:
        # 如果使用者有傳 start_time (毫秒)，轉換為奈秒 (GRVT 需求)
        # 如果沒傳，CCXT 預設通常是回傳最近的
        since = int(start_time * 1_000_000) if start_time else None

        funding_history = api.fetch_funding_rate_history(
            symbol=symbol,
            since=since,
            limit=limit,
        )
        return funding_history
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/history/account")
def get_account_history(
    limit: int = Query(20, le=100),
    api: GrvtCcxt = Depends(get_trade_api)
):
    """
    獲取帳戶資金流水/變動歷史 (Account History)
    對應原腳本: print_account_history
    """
    try:
        return api.fetch_account_history(params={"limit": limit})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))