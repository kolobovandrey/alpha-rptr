# coding: UTF-8

import json
import math
import os
import traceback
from datetime import datetime, timezone, timedelta
import time
import threading
from decimal import Decimal

import pandas as pd
from bravado.exception import HTTPNotFound
from pytz import UTC

from src import (logger, bin_size_converter, find_timeframe_string,
                 allowed_range_minute_granularity, allowed_range, sync_obj_with_config,
                 to_data_frame, resample, delta, FatalError, notify, ord_suffix)
from src import retry_bybit as retry
from pybit import inverse_futures, inverse_perpetual, usdc_perpetual, usdt_perpetual, spot
#from pybit import spot as spot_http
from src.config import config as conf
from src.exchange_config import exchange_config
from src.exchange.bybit.bybit_websocket import BybitWs

#TODO
# orderbook class
#from src.exchange.bybit.bybit_orderbook import OrderBook


class Bybit:
   # Positions in USDT?
    qty_in_usdt = False    
    # Use minute granularity?
    minute_granularity = False
    # Sort timeframes when multiple timeframes 
    timeframes_sorted = True # True for higher first, False for lower first and None when off 
    # Enable log output
    enable_trade_log = True   
    # Order Update Log
    order_update_log = True  
    # OHLCV length
    ohlcv_len = 100      
    # Call the strategy function on start. This can be useful if you don't want to wait for the candle to close
    # to trigger the strategy function. However, this can also be problematic for certain operations such as
    # sending orders or duplicates of orders that have already been sent, which were calculated based on closed
    # candle data that is no longer relevant. Be aware of these potential issues and make sure to handle them
    # appropriately in your strategy implementation. 
    call_strat_on_start = True

    def __init__(self, account, pair, demo=False, spot=False, threading=True):
        """
        constructor
        :account:
        :pair:
        :param demo:
        :param run:
        """
        # Account
        self.account = account
        # Pair
        self.pair = (pair.replace("-", "") if pair.upper().endswith("PERP") else pair).upper()
        # Spot market?
        self.spot = spot
        # Base Asset
        self.base_asset = None
        # Asset Rounding
        self.asset_rounding = None
        # Quote Asset
        self.quote_asset = None
        # Quote Rounding
        self.quote_rounding = None
        # Use testnet?
        self.demo = demo
        # Is bot running?
        self.is_running = threading
        # wallet
        self.wallet = {}
        # Position
        self.position = None
        # Position size
        self.position_size = None
        # Entry price
        self.entry_price = None
        # Margin
        self.margin = None
        # Account information
        self.account_information = None
        # Instrument
        self.instrument = {}
        # Bookticker
        self.bookticker = {}
        # Timeframe
        self.bin_size = ['1h'] 
        # Public client     
        self.public_client = None
        # Private client 
        self.private_client = None
        # Price
        self.market_price = 0
        # Order update
        self.order_update = []
        # Bar crawler
        self.crawler = None
        # Strategy
        self.strategy = None
        # OHLCV data
        self.timeframe_data = None    
        # Timeframe data info like partial candle data values, last candle values, last action etc.
        self.timeframe_info = {}
        # Profit target long and short for a simple limit exit strategy
        self.sltp_values = {
            'profit_long': 0,
            'profit_short': 0,
            'stop_long': 0,
            'stop_short': 0,
            'eval_tp_next_candle': False,
            'profit_long_callback': None,
            'profit_short_callback': None,
            'stop_long_callback': None,
            'stop_short_callback': None,
            'split': 1,
            'interval': 0
        }      
        # Is SLTP active
        self.is_sltp_active = False   
         # Profit, Loss and Trail Offset
        self.exit_order = {
            'profit': 0, 
            'loss': 0, 
            'trail_offset': 0, 
            'profit_callback': None,
            'loss_callback': None,
            'trail_callbak': None,
            'split': 1,
            'interval': 0
        }
        # Is exit order active
        self.is_exit_order_active = False
        # Trailing Stop
        self.trail_price = 0   
        # Order callbacks
        self.callbacks = {}    
        # Limit chaser order
        self.limit_chaser_ord = {'Buy': {},
                                 'Sell': {}}    
        # Best bid price
        self.best_bid_price = None
        # Best ask price
        self.best_ask_price = None 
        #  Bid quantity L1
        self.bid_quantity_L1 = None
        # Ask quantity L1
        self.ask_quantity_L1 = None

        sync_obj_with_config(exchange_config['bybit'], Bybit, self)

    def __init_client(self):
        """
        initialization of client
        """
        if self.private_client is not None and self.public_client is not None:
            return
        
        api_key = conf['bybit_test_keys'][self.account]['API_KEY'] \
                    if self.demo else conf['bybit_keys'][self.account]['API_KEY']        
        api_secret = conf['bybit_test_keys'][self.account]['SECRET_KEY'] \
                    if self.demo else conf['bybit_keys'][self.account]['SECRET_KEY']

        endpoint = 'https://api-testnet.bybit.com' if self.demo else 'https://api.bybit.com'
        # spot 
        if self.spot: 
            HTTP = spot.HTTP
            self.private_client = HTTP(endpoint, api_key=api_key, api_secret=api_secret)
            self.public_client = HTTP(endpoint)

            if self.quote_rounding == None or self.asset_rounding == None:
                markets_list = retry(lambda: self.public_client.query_symbol())   
                market = [market for market in markets_list if market.get('name')==self.pair]                
                self.quote_asset = market[0]['quoteCurrency']                      
                self.quote_rounding = abs(Decimal(str(market[0]['minPricePrecision'])).as_tuple().exponent) \
                                        if float(market[0]['minPricePrecision']) < 1 else 0 # quotePricesion??
                self.base_asset = market[0]['baseCurrency']  
                self.asset_rounding = abs(Decimal(str(market[0]['basePrecision'])).as_tuple().exponent) \
                                        if float(market[0]['basePrecision']) < 1 else 0            
        # USDC perps
        elif self.pair.endswith('PERP'): 
            HTTP = usdc_perpetual.HTTP
            self.private_client = HTTP(endpoint, api_key=api_key, api_secret=api_secret)
            self.public_client = HTTP(endpoint)

            if self.quote_rounding == None or self.asset_rounding == None:      
                markets_list = retry(lambda: self.public_client.query_symbol())   
                market = [market for market in markets_list if market.get('symbol')==self.pair]
                tick_size = float(market[0]['tickSize']) * 2 if '5' in market[0]['tickSize'] else market[0]['tickSize'] 
                self.quote_asset = market[0]['quoteCoin']                                
                self.quote_rounding = abs(Decimal(str(tick_size)).as_tuple().exponent) if float(tick_size) < 1 else 0 
                self.base_asset = market[0]['baseCoin'] 
                self.asset_rounding = abs(Decimal(str(market[0]['qtyStep'])).as_tuple().exponent) \
                                        if float(market[0]['qtyStep']) < 1 else 0  
        # USDT linear perps or inverse perps
        elif self.pair.endswith('USDT') or self.pair.endswith('USD'): 
            HTTP = usdt_perpetual.HTTP if self.pair.endswith('USDT') else inverse_perpetual.HTTP
            self.private_client = HTTP(endpoint, api_key=api_key, api_secret=api_secret)
            self.public_client = HTTP(endpoint)

            if self.quote_rounding == None or self.asset_rounding == None:      
                markets_list = retry(lambda: self.public_client.query_symbol())    
                market = [market for market in markets_list if market.get('name')==self.pair]                
                tick_size = float(market[0]['price_filter']['tick_size']) * 2 \
                            if '5' in market[0]['price_filter']['tick_size'] else market[0]['price_filter']['tick_size']                        
                self.quote_asset = market[0]['quote_currency']                                
                self.quote_rounding = abs(Decimal(str(tick_size)).as_tuple().exponent) if float(tick_size) < 1 else 0                
                self.base_asset = market[0]['base_currency']  
                self.asset_rounding = abs(Decimal(str(market[0]['lot_size_filter']['qty_step'])).as_tuple().exponent) \
                                        if float(market[0]['lot_size_filter']['qty_step']) < 1 else 0
        else:
            HTTP = inverse_futures.HTTP
        
        self.private_client = HTTP(endpoint, api_key=api_key, api_secret=api_secret)
        self.public_client = HTTP(endpoint)

        self.sync()

        logger.info(f"Asset: {self.base_asset} Rounding: {self.asset_rounding} "\
                    f"- Quote: {self.quote_asset} Rounding: {self.quote_rounding}")
         
        logger.info(f"Position Size: {self.position_size:.3f} Entry Price: {self.entry_price:.2f}")

    def sync(self):
        # Position
        if not self.spot:
            self.position = self.get_position()
        # Position size
        self.position_size = self.get_position_size()
        # Entry price
        self.entry_price = self.get_position_avg_price()
        # Market price
        self.market_price = self.get_market_price()
        # Margin
        # self.margin = self.get_margin()
        
    def now_time(self):
        """
        current time
        """
        return datetime.now().astimezone(UTC)
        
    def get_retain_rate(self):
        """
        maintenance margin
        :return:
        """
        return 0.005

    def get_lot(self, lot_leverage=1, only_available_balance=True, round_decimals=None):
        """        
        lot calculation
        :param round_decimals: round decimals
        :param lot_leverage: use None to automatically use your preset leverage
        :return:
        """
        if lot_leverage is None:
            lot_leverage = self.get_leverage()        

        balance = self.get_available_balance() if only_available_balance else self.get_balance() 
      
        if balance is None:
            logger.info(f"Can't Get Balance!")
            return balance

        return round((1 - self.get_retain_rate()) * balance
                      / (1 if self.qty_in_usdt else  self.get_market_price()) * lot_leverage,
                      round_decimals if round_decimals != None else self.asset_rounding)    

    def get_balance(self, asset=None, return_available=False):
        """
        get balance
        after the first api call it will update by api call only upon fills via execution ws stream,
        to ensure updating by api call each time pass `asset` as argument
        the default balance asset is `self.quote_asset` 
        :param asset: asset
        :param return_available: returns only available balance, since some might be used as a collateral for margin etc.        
        :return:
        """
        self.__init_client()
        if asset is None and self.margin is not None:   
            balances = self.margin
        elif asset is not None:
            balances =  self.get_all_balances()
        else:
            self.margin = self.get_all_balances()
            balances = self.margin

        if self.spot: 
            balances = balances['balances']   
            asset = asset if asset else self.quote_asset
            balance = [balance for balance in balances if balance.get('coin')==asset]           
            
            if len(balance) > 0:          
                balance = float(balance[0]['free']) if return_available else float(balance[0]['total'])                           
                return balance             
            else:
                logger.info(f"Unable to find asset: {asset} in balances")
            
        elif self.pair.endswith('USDT') or self.pair.endswith('USD'):          
            asset = asset if asset else self.quote_asset if self.pair.endswith('USDT') else self.base_asset
           
            if asset in balances:
                balance = float(balances[asset]['available_balance']) \
                             if return_available else float(balances[asset]['wallet_balance'])                
                return balance
            else:
                logger.info(f"Unable to find asset: {asset} in balances")           
        elif self.pair.endswith('PERP'):
            if asset:
                logger.info(f"Only USDC balances will be returned for this instrument")           
            balance =  float(balances['availableBalance']) if return_available else float(balances['walletBalance'])            
            return balance
        else:
            logger.info(f"Couldnt get balance, please make sure you specify asset in argument \
                        for spot and have your keys and inputs set up correctly.")
        
    def get_available_balance(self, asset=None):
        """
        get available balance, since some might be already used as a collateral for margin etc.
        :param asset: asset
        :return:
        """
        self.__init_client()
       
        available_balance = self.get_balance(asset, return_available=True)
        return available_balance       

    def get_all_balances(self):
        """
        get all balances
        :return:
        """
        self.__init_client()
       
        balances = retry(lambda: self.private_client.get_wallet_balance())       
        return balances

    # def get_margin(self):
    #     """
    #     get margin
    #     :return:
    #     """
    #     self.__init_client()
    #     if self.margin is not None:
    #         return self.margin
    #     else:  # when the WebSocket cant get it
    #         self.margin = retry(lambda: self.private_client
    #                             .User.User_getMargin(currency="XBt").result())
    #         return self.margin        
    
    def set_leverage(self, leverage, symbol=None):
        """
        set leverage
        :return:
        """
        self.__init_client()

        symbol = self.pair if symbol is None else symbol
        leverage = retry(lambda: self.private_client.set_leverage(symbol=symbol,
                                                                  leverage=leverage,
                                                                  buy_leverage=leverage,
                                                                  sell_leverage=leverage)) 
        return #self.get_leverage(symbol)

    def get_leverage(self, symbol=None):
        """
        get leverage
        :return:
        """
        self.__init_client()

        symbol = self.pair if symbol is None else symbol
        return float(self.get_position()[0]["leverage"])

    def get_position(self, symbol=None, force_api_call=False):
        """
        get the current position
        :param force_api_call: force api call
        :return:
        """
        symbol = self.pair if symbol == None else symbol

        if self.spot:
            logger.info(f"Get Position Functionality Currently Not Supported For Spot")
            return      

        def get_position_api_call():           
            ret = retry(lambda: self.private_client
                                  .my_position(symbol=symbol, category='PERPETUAL'))
            #if len(ret) > 0:           
            self.position = ret['dataList'] if self.pair.endswith('PERP') else \
                                ret if self.pair.endswith('USDT') else [ret]  
            
            if self.position is None or len(self.position) == 0:
                self.position = [{'entryPrice': 0,
                                  'size': 0,
                                  'liqPrice': 0,
                                  'side': None}]   
                return None                           
            # update so it shares certain keys between USDC perp and Inverse/Linear               
            if 'entry_price' in self.position[0]:                   
                self.position[0].update({ 'entryPrice': float(self.position[0]['entry_price']),
                                            'positionValue': float(self.position[0]['position_value']),                                             
                                            'liqPrice': float(self.position[0]['liq_price']),
                                            'bustPrice': float(self.position[0]['bust_price'])})          
            return self.position

        self.__init_client() 
       
        if self.position is not None and len(self.position) > 0 and not force_api_call:
            return self.position
        else:  # when the WebSocket cant get it or forcing the api call is needed
            position = get_position_api_call()       
            return position

    def get_position_size(self, force_api_call=False):
        """
        get position size
        :param force_api_call: force api call
        :return:
        """
        self.__init_client()
        if self.spot: # Spot treats positions only as changes in balance from quote asset to base asset           
            position = self.get_balance(asset=self.base_asset) 
            position = 0 if position is None else position
            return float(position)

        position = self.get_position(force_api_call=force_api_call)

        if position is not None: # Positition size may return positive even when short !!!          
            position_size = float(position[0]['size'])           
            position_size = -position_size if position[0]['side'] == 'Sell' else position_size 
            return position_size  
        else:
            return 0

    def get_position_avg_price(self):
        """
        get average price of the current position
        :return:
        """
        self.__init_client()
        
        position = self.get_position()
        if position is None or len(position) == 0:
            return 0
         
        position_avg_price = float(position[0]['entryPrice']) \
                            if self.pair.endswith('PERP') else float(position[0]['entry_price']) 
        
        return position_avg_price 

    def get_market_price(self):
        """
        get current price
        :return:
        """
        self.__init_client()
        if self.market_price != 0:
            return self.market_price
        else:  # when the WebSocket cant get it
            symbol_information = self.get_latest_symbol_information()
            if symbol_information is None:
                return 0
            if self.spot or self.pair.endswith('PERP'):
                self.market_price = float(symbol_information['lastPrice'])       
            elif self.pair.endswith('USDT') or self.pair.endswith('USD'):
                self.market_price = float(symbol_information[0]['last_price'])             
            
            return self.market_price
    
    def get_pnl(self):
        """
        get profit and loss calculation in %
        :return:
        """
        # PnL calculation in %            
        pnl = self.get_profit()* 100/self.get_balance()
        return pnl   

    def get_profit(self, close=None, avg_entry_price=None, position_size=None, commission=None):

        if close is None:
            close = self.get_market_price() 
        if avg_entry_price is None:
            avg_entry_price = self.entry_price if self.entry_price != None else self.get_position_avg_price()
        if position_size is None:
            position_size = self.get_position_size()
        if commission is None:
            commission = self.get_commission()

        profit = 0
        close_rate = 0

        if position_size > 0:
            close_rate = ((close - avg_entry_price)/avg_entry_price) - commission                 
        elif (position_size < 0):
            close_rate = ((avg_entry_price - close)/avg_entry_price) - commission
        
        profit = round(abs(position_size) 
                        * close_rate * (1 if self.qty_in_usdt else avg_entry_price), self.quote_rounding)

        return profit
    
    def get_latest_symbol_information(self, symbol=None):
        """
        get latest symbol(trading pair) information
        :param symbol: if provided it will return information for the specific symbol otherwise,
            otherwise it returns values for the pair currently traded 
        :return:
        """
        symbol = self.pair if symbol == None else symbol   
        try:     
            latest_symbol_information = retry(lambda: self.public_client.latest_information_for_symbol(symbol=symbol))
        except Exception  as e:        
            logger.info(f"An error occured: {e}")
            logger.info(f"Sorry couldnt retrieve information for symbol: {symbol}")
            return None
        
        return latest_symbol_information
    
    def get_orderbook(self):
        """
        get orderbook L2, therefore best bid and best ask prices
        """
        self.__init_client()
        ob =  retry(lambda: self.public_client
                                      .orderbook(symbol=self.pair))
        self.best_bid = float(ob[0]['price'])
        self.best_ask = float(ob[1]['price'])
        
    def get_trail_price(self):
        """
        get Trail Price。
        :return:
        """
        return self.trail_price

    def set_trail_price(self, value):
        """
        set Trail Price
        :return:
        """
        self.trail_price = value

    def get_commission(self):
        """
        get commission
        :return:
        """
        return 0.075 / 100    #
    
    def cancel_all(self, only_active=False, only_conditional=False):
        """
        cancel all orders for this pair
        """
        self.__init_client()  
        
        res_active_orders = None   
        res_conditional_orders = None

        if self.spot:            
            res_active_orders = self.cancel_active_order(cancel_all=True)
        else:           
            res_active_orders = self.cancel_active_order(cancel_all=True) if not only_conditional else None          
            res_conditional_orders = self.cancel_conditional_order(cancel_all=True) if not only_active else None
        
        if res_active_orders and res_conditional_orders:
            logger.info(f"Cancelled All Orders For: {self.pair}")
        if res_active_orders:
            logger.info(f"Cancelled All Active Orders For: {self.pair}")     
        if res_conditional_orders:
            logger.info(f"Cancelled All Conditional Orders For: {self.pair}")
        if res_active_orders == False or res_conditional_orders == False:
            logger.info(f"There Was An Issue While Trying To Cancel All Conditional Orders For This Pair: {self.pair}")

        self.callbacks = {} 
    
    def close_all(self, 
                  spot_safety_catch=True, 
                  callback=None, 
                  split=1, interval=0, 
                  limit_chase_init_delay=0.0001, chase_update_rate=0.05, limit_chase_interval=0):
        """
        market close open position for this pair
        :params spot_safety_catch: this is here to prevent you to accidentally dump all your base asset,
        -because spot treats positions as changes in balance of base asset and quote asset, there is no open position status
        :param callback:
        :param split:
        :param interval:
        :param limit_chase_init_delay:
        :param chase_update_rate:
        :paran limit_chase_interval: greater than 0 starts limit chase order
        """
        self.__init_client()
        position_size = self.get_position_size()
        if position_size == 0 or (spot_safety_catch and self.spot):
            return

        side = False if position_size > 0 else True
        
        self.order("Close", side, abs(position_size), 
                   post_only=bool(limit_chase_interval),
                   limit_chase_init_delay=limit_chase_init_delay,
                   chase_update_rate=chase_update_rate, 
                   limit_chase_interval=limit_chase_interval, 
                   callback=callback, 
                   split=split, 
                   interval=interval)
        position_size = self.get_position_size()
        if position_size == 0:
            logger.info(f"Closed {self.pair} position")
        else:
            logger.info(f"Failed to close all {self.pair} position, still {position_size} amount remaining")

    def cancel(self, id):
        """
        Cancel a specific active order by id
        - its not going to query orders and filter them prior
        :param id: id of the order (user id)
        :return: result
        """
        self.__init_client()
    
        orders = self.get_open_orders(id, separate=True)
        #logger.info(f"orders: {orders}")

        if self.pair.endswith('PERP') or self.spot: # 'orderId','orderLinkId'             
            order_link_id = 'orderLinkId'
        else: # Inverse and Linear perps           
            order_link_id = 'order_link_id'

        if orders is None:
            logger.info(f"Couldn't find an order of which id string starts with: {id}")
            return False       
        
        if len(orders['active_orders']) > 0:
            order = orders['active_orders'][0]
            res = self.cancel_active_order(user_id=order[order_link_id])
        else:
            order = orders['conditional_orders'][0]
            res = self.cancel_conditional_order(user_id=order[order_link_id])
        if res:
            return res

    def cancel_conditional_order(self, user_id=None, order_id=None, cancel_all=False):
        """
        Cancel a specific conditional order by id - in this function you have to provide full id,
        because its not going to query orders and filter them prior
        - for conveniece reasons its recommended to use `cancer()` for a single 
          and `cancel_all()` for cancelling all orders conditional and active
        :param order_id: an id of ab order given interally to the order by the exchange
        :param user_id: your user id that you give to an order(given upon sending it)
        :param cancel_all: cancel all conditional orders for this pair
        :return: result - boolean
        """
        if order_id == None and user_id == None and not cancel_all:
            logger.info(f"No id was provided, unable to cancel an order!")
            return False
        self.__init_client()
        if self.spot:
            logger.info(f"Cannot Cancel Conditional Orders on Spot currently")
            return
        # USDC Perp
        if self.pair.endswith('PERP'): # 'orderId','orderLinkId'    
            order_id = order_id if order_id else "" # cannot be None, we have to pass and empty string for USDC PERP
            user_id = user_id if user_id else ""      
        #if self.pair.endswith('USDT'):            
        stop_order_id = order_id        
        # Inverse, Linear Perps and USDC perp 
        if self.pair.endswith('PERP'):
            cancel_conditional = self.private_client.cancel_all_active_orders \
                                if cancel_all else self.private_client.cancel_active_order
        else:
            cancel_conditional = self.private_client.cancel_all_conditional_orders \
                                if cancel_all else self.private_client.cancel_conditional_order

        res = retry(lambda: cancel_conditional(order_id=order_id, order_link_id=user_id, 
                                                orderId=order_id, orderLinkId=user_id, 
                                                stop_order_id=stop_order_id, 
                                                symbol=self.pair, orderFilter='StopOrder'))                 

        if 'status' in res and res['status'].upper() == 'CANCELED':
            logger.info(f"Already Cancelled - status: {res['status']}")
            return False        
        #return True
        # TODO exception pybit.exceptions.InvalidRequestError: Order not exists or too late to repalce (ErrCode: 20001) (ErrTime: 22:40:43). etc...
        if ('orderId' not in res  or 'orderLinkId' not in res) and self.pair.endswith('PERP'):           
            return False
        else:
            logger.info(f"Cancelling order usder id: {user_id} order id: {order_id} pair: {self.pair}") # response: {res}")
        if user_id is not None:
            self.callbacks.pop(user_id)
        return True      
    
    def cancel_active_order(self, order_id=None, user_id=None, cancel_all=False):
        """
        Cancel a specific active order by id - in this function you have to provide full id,
        because its not going to query orders and filter them prior
        -for conveniece reasons its recommended to use `cancer()` and `cancel_all()`
        :param order_id: an id of ab order given interally to the order by the exchange
        :param user_id: your user id that you give to an order(given upon sending it)
        :param cancel_all: cancel all active orders for this pair 
        :return: result - boolean
        """
        if order_id == None and user_id == None and not cancel_all:
            logger.info(f"No id was provided, unable to cancel an order!")
            return False
        self.__init_client()
        # USDC Perp
        if self.pair.endswith('PERP'): # 'orderId','orderLinkId'    
            order_id = order_id if order_id else "" # cannot be None, we have to pass and empty string for USDC PERP
            user_id = user_id if user_id else ""          
        
        # Spot, Inverse, Linear Perps and USDC perp       
        cancel_active = self.private_client.cancel_all_active_orders \
                        if not self.spot else self.private_client.batch_cancel_active_order \
                            if cancel_all else self.private_client.cancel_active_order
        
        res =  retry(lambda: cancel_active(order_id=order_id, order_link_id=user_id,
                                 orderId=order_id, orderLinkId=user_id,
                                   symbol=self.pair, orderFilter='Order',
                                     orderTypes="LIMIT,LIMIT_MAKER"))        

        if 'status' in res and res['status'].upper() == 'CANCELED':
            logger.info(f"Already Cancelled - status: {res['status']}")
            return False       
       
        # TODO exception pybit.exceptions.InvalidRequestError: Order not exists or too late to repalce (ErrCode: 20001) (ErrTime: 22:40:43). etc...
        if ('orderId' not in res  or 'orderLinkId' not in res) and self.pair.endswith('PERP'):           
            return False
        else:
            logger.info(f"Cancelling order usder id: {user_id} order id: {order_id} pair: {self.pair}") #- response: {res}")
        if user_id is not None:
            self.callbacks.pop(user_id)
        return True      

    def __new_order(
        self,
        ord_id,
        side,
        ord_qty,
        limit=0,
        stop=0,
        post_only=False,
        reduce_only=False,
        trailing_stop=0, 
        activationPrice=0,
        trigger_by='LastPrice'
    ):
        """
        create an order
        """          
        if self.spot and stop > 0:
            logger.info(f"Conditional Orders Not Yet supported For Spot According To Official Bybit API.")

        ord_qty = str(ord_qty) if self.pair.endswith('PERP') else ord_qty # USDC perps require strings for order quanity and price etc...       
       
        market_price = self.get_market_price()
        base_price = str(self.market_price) if self.pair.endswith('PERP') else self.market_price 
                    #market_price + market_price / 100 * 1 if stop > market_price else market_price - market_price / 100 * 1        
      
        # USDC perps dont have condittional orders API endpoints
        place_conditional = self.private_client.place_active_order \
                                if self.pair.endswith('PERP') else self.private_client.place_conditional_order      
       
        if limit > 0 and post_only:            
            ord_type = "Limit" 
            type = "LIMIT_MAKER" # Spot only
            orderFilter = "Order" if self.pair.endswith('PERP') else None             
            limit = str(limit) if self.pair.endswith('PERP') else limit 
            #limit = str(limit) if self.pair.endswith('PERP') else limit 
            if self.spot:    
                res = retry(lambda: self.private_client.place_active_order(symbol=self.pair, type=type,
                                                                           orderLinkId=ord_id,
                                                                           side=side,
                                                                           qty=ord_qty,
                                                                           price=limit,
                                                                           timeInForce='GTC'))              
            else:
                res = retry(lambda: self.private_client
                                .place_active_order(symbol=self.pair, order_type=ord_type, orderType=ord_type, type=type,
                                                    order_link_id=ord_id, orderLinkId=ord_id, side=side, 
                                                    qty=ord_qty, orderQty=ord_qty,
                                                    price=limit, orderPrice=limit,
                                                    reduce_only=reduce_only, reduceOnly=reduce_only,
                                                    close_on_trigger=reduce_only,
                                                    time_in_force='PostOnly', timeInForce='PostOnly',
                                                    orderFilter=orderFilter, position_idx=0))                
        elif limit > 0 and stop > 0:
            ord_type = "Limit" #"StopLimit"
            #type = "LIMIT" #Spot only
            orderFilter = "StopOrder" if self.pair.endswith('PERP') else None
            stop = str(stop) if self.pair.endswith('PERP') else stop 
            limit = str(limit) if self.pair.endswith('PERP') else limit 
            res = retry(lambda: place_conditional(symbol=self.pair, order_type=ord_type, orderType=ord_type,
                                                  order_link_id=ord_id, orderLinkId=ord_id, side=side,
                                                  qty=ord_qty, orderQty=ord_qty, price=limit, stop_px=stop, triggerPrice=stop,
                                                  orderPrice=limit, base_price=base_price, basePrice=base_price,
                                                  reduce_only=reduce_only,  reduceOnly=reduce_only,
                                                  close_on_trigger=reduce_only,                                                                           
                                                  time_in_force='GoodTillCancel', timeInForce='GoodTillCancel',
                                                  trigger_by=trigger_by, triggerBy=trigger_by,
                                                  orderFilter=orderFilter, position_idx=0))
        elif limit > 0:
            ord_type = "Limit" 
            type = "LIMIT"
            orderFilter = "Order" if self.pair.endswith('PERP') else None 
            limit = str(limit) if self.pair.endswith('PERP') else limit   
            if self.spot:    
                res = retry(lambda: self.private_client.place_active_order(symbol=self.pair, type=type,
                                                                           orderLinkId=ord_id,
                                                                           side=side,
                                                                           qty=ord_qty,
                                                                           price=limit, 
                                                                           timeInForce='GTC'))            
            else:
                res = retry(lambda: self.private_client
                            .place_active_order(symbol=self.pair, order_type=ord_type, orderType=ord_type, type=type,
                                                order_link_id=ord_id, orderLinkId=ord_id, side=side,
                                                qty=ord_qty, orderQty=ord_qty, price=limit, orderPrice=limit,
                                                reduce_only=reduce_only, reduceOnly=reduce_only,
                                                close_on_trigger=reduce_only,
                                                time_in_force='GoodTillCancel', timeInForce='GoodTillCancel',
                                                orderFilter=orderFilter, position_idx=0))
        elif stop > 0:
            ord_type = "Market" #"Stop"
            #type = "MARKET" #Spot only
            orderFilter = "StopOrder" if self.pair.endswith('PERP') else None
            limit = str(limit) if self.pair.endswith('PERP') else limit   
            stop = str(stop) if self.pair.endswith('PERP') else stop    
            res = retry(lambda: place_conditional(symbol=self.pair, order_type=ord_type, orderType=ord_type,
                                                  order_link_id=ord_id, orderLinkId=ord_id, side=side,
                                                  qty=ord_qty, orderQty=ord_qty, stop_px=stop, triggerPrice=stop,
                                                  reduce_only=reduce_only, reduceOnly=reduce_only,
                                                  close_on_trigger=reduce_only,
                                                  orderPrice=limit, base_price=str(base_price), basePrice=base_price,
                                                  time_in_force='GoodTillCancel', timeInForce='GoodTillCancel',
                                                  trigger_by=trigger_by, triggerBy=trigger_by,
                                                  orderFilter=orderFilter, position_idx=0))
        elif post_only:  # Limit Chaser        
            limit = self.best_bid_price if side == "Buy" else self.best_ask_price
            ord_type = "Limit" 
            type = "LIMIT_MAKER" # Spot only
            orderFilter = "Order" if self.pair.endswith('PERP') else None             
            limit = str(limit) if self.pair.endswith('PERP') else limit 
           
            def chaser(ord_id=ord_id):
                i=0
                while True:
                    #logger.info(f"{self.limit_chaser_ord}")
                    if self.limit_chaser_ord[side]['is_amend_active'] \
                        or not self.limit_chaser_ord[side]['is_active']: 
                        self.limit_chaser_ord[side]['active_counter'] = 0             
                        return                    
                    
                    ord_id = self.limit_chaser_ord[side]['ID'].split('_')[0] \
                             + '_' + str(i) + ord_suffix() if i>0 else ord_id              
                    
                    limit = self.best_bid_price if side == "Buy" else self.best_ask_price
                    #logger.info(f"i: {i}   active_counter: {self.limit_chaser_ord[side]['active_counter']}")
                    if limit is None \
                        or self.limit_chaser_ord[side]['active_counter'] != i:
                        time.sleep(0.05)
                        continue
                    limit = str(limit) if self.pair.endswith('PERP') else limit   
                    #logger.info(f"best bid: {self.best_bid_price}     best ask: {self.best_ask_price} ")                   

                    self.limit_chaser_ord[side]['ID'] = ord_id      
                    ord_qty = abs(self.limit_chaser_ord[side]['Qty']) - self.limit_chaser_ord[side]['Filled']
                    ord_qty = str(ord_qty) if self.pair.endswith('PERP') else ord_qty
                    if self.spot:    
                        res = retry(lambda: self.private_client
                                    .place_active_order(symbol=self.pair, type=type,
                                                        orderLinkId=ord_id,
                                                        side=side,
                                                        qty=ord_qty,
                                                        price=limit,
                                                        timeInForce='GTC'))      
        
                    else:
                        res = retry(lambda: self.private_client
                                    .place_active_order(symbol=self.pair, 
                                                        order_type=ord_type, orderType=ord_type, type=type,
                                                        order_link_id=ord_id, orderLinkId=ord_id, side=side, 
                                                        qty=ord_qty, orderQty=ord_qty,
                                                        price=limit, orderPrice=limit,
                                                        reduce_only=reduce_only, reduceOnly=reduce_only,
                                                        close_on_trigger=reduce_only,
                                                        time_in_force='PostOnly', timeInForce='PostOnly',
                                                        orderFilter=orderFilter, position_idx=0))
                    time.sleep(0.1)
                    i += 1   

            threading.Timer(0.001, chaser).start()       
        else:
            ord_type = "Market"
            type = "MARKET"
            orderFilter = "Order" if self.pair.endswith('PERP') else None
            stop = str(stop) if self.pair.endswith('PERP') else stop      
            if self.spot:    
                res = retry(lambda: self.private_client
                                            .place_active_order(symbol=self.pair, type=type,
                                                                orderLinkId=ord_id,
                                                                side=side,
                                                                qty=ord_qty,                                                                           
                                                                timeInForce='GTC'))   
            else:   
                res = retry(lambda: self.private_client
                            .place_active_order(symbol=self.pair, order_type=ord_type, type=type, orderType=ord_type,
                                                order_link_id=ord_id, orderLinkId=ord_id, side=side,
                                                qty=ord_qty, orderQty=ord_qty,
                                                reduce_only=reduce_only, reduceOnly=reduce_only,
                                                close_on_trigger=reduce_only,
                                                time_in_force='GoodTillCancel', timeInForce='GoodTillCancel',
                                                orderFilter=orderFilter, position_idx=0))

        if self.enable_trade_log:
            logger.info(f"========= New Order ==============")
            logger.info(f"ID     : {ord_id}")
            logger.info(f"Type   : {ord_type}")
            logger.info(f"Side   : {side}")
            logger.info(f"Qty    : {ord_qty}")
            logger.info(f"Limit  : {limit}")
            logger.info(f"Stop   : {stop}")
            logger.info(f"======================================")

            notify(f"New Order\nType: {ord_type}\nSide: {side}\nQty: {ord_qty}\nLimit: {limit}\nStop: {stop}")

    def amend_order(self, ord_id, ord_qty=0, limit=0, stop=0):
        """
        Amend order with querying the order prior verifying its existence and whether its active or conditional etc.
        """
        if self.spot:
            logger.info(f"Amending Orders Is Not Supported For Spot Yet.")

        kwargs = {k: v  for k, v in locals().items() if v and k != 'self' and k != 'ord_id'}
        
        orders = self.get_open_orders(id=ord_id, separate=True)

        if orders is None or (len(orders['active_orders']) == 0 and len(orders['conditional_orders']) == 0):
            logger.info(f"Cannot Find An Order to Amend Id: {ord_id}")
            return
        
        if self.pair.endswith('PERP') or self.spot: # 'orderId','orderLinkId'             
            order_link_id = 'orderLinkId'
        else: # Inverse and Linear perps           
            order_link_id = 'order_link_id'     

        is_active_order = True if len(orders['active_orders']) > 0 or self.pair.endswith('PERP') else False
        order = orders['active_orders'][0] if is_active_order else orders['conditional_orders'][0]
        ord_id = order[order_link_id]
        orderFilter= order['orderType'] if 'orderType' in order else None
        
        for k,v in kwargs.items():   
            kwarg = {k: v}     
            res = self.__amend_order(ord_id, not is_active_order, **kwarg)
           
    def __amend_order(self, ord_id, is_conditional, **kwargs):
        """
        amend order provided full user id and whether its condition or not
        - keep in mind its designed to take only one keyed argument,
            since bybit only allows us one paramater(price, qty, stop...) to amend each time
        """         
        if len(kwargs) == 0:
            logger.info(f"No kwargs were provided.")
            return    

        for k,v in kwargs.items():
            if k == 'limit':
                kwargs = {'p_r_price': v, 'orderPrice': str(v)} # we are gonna be using string values for USDC perps, no conflict with spot here since no spot amending possible yet
            if k == 'ord_qty':
                kwargs = {'p_r_qty': v, 'orderQty': str(v)}
            if k == 'stop':
                kwargs = {'p_r_trigger_price': v, 'triggerPrice':str(v)}

        res = None

        if is_conditional:
            res = retry(lambda: self.private_client
                        .replace_conditional_order(symbol=self.pair, order_link_id=ord_id,
                                                   orderLinkId=ord_id, orderFilter='StopOrder',
                                                   **kwargs))
        else:
            res = retry(lambda: self.private_client
                        .replace_active_order(symbol=self.pair, order_link_id=ord_id,
                                              orderLinkId=ord_id, orderFilter='Order',
                                              **kwargs))

        if self.enable_trade_log:
            logger.info(f"========= Amend Order ==============")
            logger.info(f"ID       : {ord_id}")            
            logger.info(f"======================================")
            notify(f"Amend Order\n ID       : {ord_id}")      

        if res:                
            logger.info(f"Modified Order with user_id: {ord_id}, response: {res}")
            return res         

    def entry(
        self,
        id,
        long,
        qty,
        limit=0,
        stop=0,
        post_only=False,
        reduce_only=False,
        when=True,
        round_decimals=None,
        callback=None,
        trigger_by='LastPrice',
        split=1,
        interval=0,
        limit_chase_init_delay=0.0001, 
        chase_update_rate=0.05, 
        limit_chase_interval=0
    ):
        """
        places an entry order, works as equivalent to tradingview pine script implementation
        https://tradingview.com/study-script-reference/#fun_strategy{dot}entry
        :param id: Order id
        :param long: Long or Short
        :param qty: Quantity
        :param limit: Limit price
        :param stop: Stop limit
        :param post_only: Post only
        :param reduce_only: your existing position cannot be increased only reduced by this order
        :param when: Do you want to execute the order or not - True for live trading
        :param round_decimals: round_decimals - if not provided its rounded automatically
        :param callback:
        :param trigger_by: what price to use for triggers
        :param split: for iceberg order
        :param inerval: for iceberg order
        :param limit_chase_init_delay: for limit order chasing
        :param chase_update_rate: limit order chasing sleep interval between price updates etc
        :param limit_chase_interval: has to be above 0 to start limit chasing along with `post_only`
        :return:
        """
        self.__init_client()

        # if self.get_margin()['excessMargin'] <= 0 or qty <= 0:
        #     return

        if not when:
            return

        pos_size = self.get_position_size()
        logger.info(f"pos_size: {pos_size}")

        if long and pos_size > 0:
            return

        if not long and pos_size < 0:
            return

        ord_qty = abs(qty) + abs(pos_size)
        ord_qty = round(ord_qty, round_decimals if round_decimals != None else self.asset_rounding)

        self.order(id, long, ord_qty, limit, stop, post_only, 
                   reduce_only, when, callback, trigger_by, split, interval,
                   limit_chase_init_delay, chase_update_rate, limit_chase_interval)

    def entry_pyramiding(
        self,
        id,
        long,
        qty,
        limit=0,
        stop=0,
        trailValue= 0,
        post_only=False,
        reduce_only=False,
        cancel_all=False,
        pyramiding=2,
        when=True,
        round_decimals=None,
        callback=None,
        trigger_by='LastPrice',
        split=1,
        interval=0,
        limit_chase_init_delay=0.0001, 
        chase_update_rate=0.05, 
        limit_chase_interval=0
    ):
        """
        places an entry order, works as equivalent to tradingview pine script implementation with pyramiding
        https://tradingview.com/study-script-reference/#fun_strategy{dot}entry
        :param id: Order id
        :param long: Long or Short
        :param qty: Quantity
        :param limit: Limit price
        :param stop: Stop limit
        :param post_only: Post only
        :param reduce_only: Reduce Only means that your existing position cannot be increased only reduced by this order
        :param cancell_all: cancell all open order before sending the entry order?
        :param pyramiding: number of entries you want in pyramiding
        :param when: Do you want to execute the order or not - True for live trading
        :param round_decimals: round_decimals - if not provided its rounded automatically
        :param callback:
        :param trigger_by: what price to use for triggers
        :param split: for iceberg order
        :param inerval: for iceberg order
        :param limit_chase_init_delay: for limit order chasing
        :param chase_update_rate: limit order chasing sleep interval between price updates etc
        :param limit_chase_interval: has to be above 0 to start limit chasing along with `post_only`
        :return:
        """ 
        # if self.get_margin()['excessMargin'] <= 0 or qty <= 0:
        #     return
        if qty <= 0:
            return

        if not when:
            return

        pos_size = self.get_position_size()

        if long and pos_size >= pyramiding*qty:
            return

        if not long and pos_size <= -(pyramiding*qty):
            return
        
        if cancel_all:
            self.cancel_all()   

        if (long and pos_size < 0) or (not long and pos_size > 0):
            ord_qty = qty + abs(pos_size)        
        else:
            ord_qty = qty  
        
        if (long and pos_size + qty > pyramiding*qty) or (not long and pos_size - qty < -pyramiding*qty):
            ord_qty = pyramiding*qty - abs(pos_size)
     
        # make sure it doesnt spam small entries, which in most cases would trigger risk management orders evaluation,
        # you can make this less than 2% if needed  
        if ord_qty < ((pyramiding*qty) / 100) * 2:
            return       

        ord_qty = round(ord_qty, round_decimals if round_decimals != None else self.asset_rounding)

        self.order(id, long, ord_qty, limit, stop, post_only, 
                   reduce_only, when, callback, trigger_by, split, interval,
                   limit_chase_init_delay, chase_update_rate, limit_chase_interval)

    def order(
        self,
        id,
        long,
        qty,
        limit=0,
        stop=0,
        post_only=False,
        reduce_only=False,
        when=True,
        round_decimals=None,
        callback=None,
        trigger_by='LastPrice',
        split=1,
        interval=0, 
        limit_chase_init_delay=0.0001, 
        chase_update_rate=0.05, 
        limit_chase_interval=0
    ):
        """
        places an order, works as equivalent to tradingview pine script implementation
        https://www.tradingview.com/pine-script-reference/#fun_strategy{dot}order
        :param id: Order id
        :param long: Long or Short
        :param qty: Quantity
        :param limit: Limit price
        :param stop: Stop limit
        :param post_only: Post only 
        :param reduce_only: your existing position cannot be increased only reduced by this order       
        :param when: Do you want to execute the order or not - True for live trading
        :param callback:
        :param trigger_by: what price to use for triggers
        :param split: for iceberg order
        :param inerval: for iceberg order
        :param limit_chase_init_delay: for limit order chasing
        :param chase_update_rate: limit order chasing sleep interval between price updates etc
        :param limit_chase_interval: has to be above 0 to start limit chasing along with `post_only`
        :return:
        """
        self.__init_client()

        # if self.get_margin()['excessMargin'] <= 0 or qty <= 0:
        #     return

        if not when:
            return

        side = "Buy" if long else "Sell" 
        ord_qty = abs(round(qty, round_decimals if round_decimals != None else self.asset_rounding))
        order = self.get_open_order(id)
        ord_id = id + ord_suffix() #if order is None else order["clientOrderId"]

        if split > 1:

            exchange = self
            sub_ord_qty = round(ord_qty/split, self.asset_rounding)
            
            class split_order:

                def __init__(self,count):
                    self.count = count

                def __call__(self):
                    logger.info(f"Split Order - Filled - {self.count}/{split}")
                    threading.Timer(interval, self.next_order).start()

                def next_order(self):  

                    sub_ord_id = f"{id}_sub{self.count+1}"                  

                    #last sub order
                    if(self.count == split-1):     
                        #remaining quantity                   
                        s_ord_qty = round(ord_qty - sub_ord_qty*(split-1), exchange.asset_rounding)
                        def final_callback():
                            logger.info(F"Order ID - {id} - All Suborders filled!")
                            if callable(callback):
                                callback() #call original callback
                        sub_ord_callback = final_callback 
                    else:
                        s_ord_qty = sub_ord_qty
                        sub_ord_callback = type(self)(self.count+1)
                    
                    # Override stop for subsequent sub orders
                    exchange.order(sub_ord_id, long, s_ord_qty, limit, 0, post_only,
                                    reduce_only, trigger_by=trigger_by, callback=sub_ord_callback)

            sub_ord_id = f"{id}_sub1"
            self.order(sub_ord_id, long, sub_ord_qty, limit, stop, post_only,
                        reduce_only, trigger_by=trigger_by, callback=split_order(1))
            return
        
        if limit_chase_interval>0:           
            self.limit_chaser_ord[side] = {'ID': ord_id,
                                           'Status': '',
                                           'Qty': ord_qty,
                                           'Filled': 0,
                                           'Limit': 0,
                                           'active_counter': 0,
                                           'chase_counter': 0,
                                           'is_active': True,
                                           'is_amend_active': False,                                           
                                           'callback': callback}
            
            first_ord_res = self.__new_order(ord_id=ord_id, 
                                             side=side, 
                                             ord_qty=ord_qty, 
                                             post_only=post_only, 
                                             reduce_only=reduce_only)           

            def amend_chaser():      
                i=1 
                while True:                 
                    if not self.limit_chaser_ord[side]['is_amend_active'] \
                        and not self.limit_chaser_ord[side]['is_active']:
                        return
           
                    ord_id = self.limit_chaser_ord[side]['ID']
                    limit = self.best_bid_price if side == "Buy" else self.best_ask_price 

                    if limit is None:
                        time.sleep(chase_update_rate)
                        continue
                         
                    if 'Status' in self.limit_chaser_ord[side] \
                        and self.limit_chaser_ord[side]['Status'] == 'Cancelled' \
                        and self.limit_chaser_ord[side]['chase_counter'] > 0:
                        ord_qty = abs(self.limit_chaser_ord[side]['Qty']) #- self.limit_chaser_ord[side]['Filled']
                   
                        self.__new_order(ord_id=ord_id.split('_')[0] + ord_suffix(),  # Sending a replacement order 
                                         side=side, ord_qty=ord_qty,                  # When amending fails
                                         post_only=post_only, 
                                         reduce_only=reduce_only)  
                                               
                        self.limit_chaser_ord[side]['callback'] = callback 
                        self.limit_chaser_ord[side]['chase_counter'] = 0         
                        i=1             
                        #time.sleep(0.05)
                        continue                        
                    
                    if limit == self.limit_chaser_ord[side]['Limit'] \
                        or self.limit_chaser_ord[side]['chase_counter'] != i:  # Checking if the price has changed
                        #logger.info(f"best bid: {self.best_bid_price}     best ask: {self.best_ask_price}")                      
                        time.sleep(chase_update_rate)
                        continue                   

                    if not self.limit_chaser_ord[side]['is_amend_active']:
                        time.sleep(chase_update_rate)
                        continue

                    limit = str(limit) if self.pair.endswith('PERP') else limit  
                    res = self.__amend_order(ord_id, is_conditional=False, limit=limit) # Amend the order
                    time.sleep(limit_chase_interval)
                    i+=1
            threading.Timer(limit_chase_init_delay, amend_chaser).start()
            return

        self.callbacks[ord_id] = callback

        if order is None:
            self.__new_order(ord_id, side, ord_qty, limit, stop, post_only, reduce_only, trigger_by)
        else:
            self.__new_order(ord_id, side, ord_qty, limit, stop, post_only, reduce_only, trigger_by)
            #self.__amend_order(ord_id, side, ord_qty, limit, stop, post_only)
            return        

    def get_open_order_qty(self, id, only_active=False, only_conditional=False):
        """        
        Returns the order quantity of the first open order that starts the given order ID.
        :param id: The ID of the order to search for
        :param only_active: return qty of active orders only   
        :param only_conditional: return qty of conditonal orders only  
        :return: The quantity of the first open order or None if no matching order is found
        """        
        quantity_str = ["origQty", "qty"]#"leaves_qty", "leavesQty"]
        order = self.get_open_order(id=id,only_active=only_active, only_conditional=only_conditional)        
        
        order_qty = [] if order is None else [float(order[q]) for q in quantity_str if q in order]   
        return order_qty[0] if order_qty else None

    def get_open_order(self, id, only_active=False, only_conditional=False):
        """
        Returns the order of the first open order that starts with the given order ID.
        :param id: order id  - returns only first order from the list of orders that will match the id,
                     since it looks if the id starts with the string you pass as `id`
        :param only_active: return active orders only      
        :param only_conditional: return conditonal orders only  
        :return: The first open order that matches the given order ID, or None if no matching order is found
        """        
        orders = self.get_open_orders(id=id,only_active=only_active, only_conditional=only_conditional)
        return orders[0] if orders else None
    
    def get_open_orders(self, id=None, only_active= False, only_conditional=False, separate=False):
        """
        Get all orders or only all conditional orders        
        :param id: if provided it will return only those that start with the provided string
        :param: only_active: return only active
        :param: only_conditional: return only conditional
        :param: separate: returns a dictionary containing separate keys for active and conditional orders                
        :return: list of open orders or None
        """
        self.__init_client()
            # Spot
        if self.spot:
            # Unfortunately there is not conditional functionality in their API documentation currently for spot
            #exch_ord_id = 'orderId'
            user_id = 'orderLinkId'
            active_orders = retry(lambda: self.private_client
                                 .query_active_order(symbol= self.pair))
            conditional_orders = []
            # USDC perps                                
        elif self.pair.endswith('PERP'): 
            #exch_ord_id = 'orderId'
            user_id = 'orderLinkId'
            # Active orders include conditional orders in USDC perps for some reason  
            active_orders = retry(lambda: self.private_client
                                .get_active_order(symbol= self.pair, category="PERPETUAL"))['dataList'] # ['dataList' if self.pair.endswith('PERP') else 'data']          
            conditional_orders = [order for order in active_orders if order['stopOrderType'] == 'Stop']            
        else:
            # Inverse or USDT linear
            #exch_ord_id = 'orderId'
            user_id = 'order_link_id'            
            active_orders = retry(lambda: self.private_client
                                .query_active_order(symbol= self.pair))
            conditional_orders = retry(lambda: self.private_client
                                .query_conditional_order(symbol= self.pair))       
        
        orders = conditional_orders if only_conditional else active_orders if only_active else \
                                    {
                                    'active_orders': active_orders,
                                    'conditional_orders': conditional_orders
                                    } if separate else [*active_orders, *conditional_orders]
        
        if id is not None:
            if separate:
                orders = {
                    'active_orders': [order for order in orders['active_orders'] if order[user_id].startswith(id)],
                    'conditional_orders': [order for order in orders['conditional_orders'] if order[user_id].startswith(id)]
                }
            else:
                orders = [order for order in orders if order[user_id].startswith(id)] 

        if separate and (len(orders['active_orders']) > 0 or len(orders['conditional_orders']) > 0):
            return orders
        elif len(orders) > 0:                            
            return orders        
        else:
            return None

    def exit(
        self,
        profit=0,
        loss=0,
        trail_offset=0,
        profit_callback=None,
        loss_callback=None,
        trail_callback=None,
        split=1,
        interval=0
    ):
        """
        profit taking and stop loss and trailing, 
        if both stop loss and trailing offset are set trailing_offset takes precedence
        :param profit: Profit 
        :param loss: Stop loss 
        :param trail_offset: Trailing stop price
        """
        self.exit_order = {
            'profit': profit, 
            'loss': loss, 
            'trail_offset': trail_offset, 
            'profit_callback': profit_callback,
            'loss_callback': loss_callback,
            'trail_callback': trail_callback,
            'split': split,
            'interval': interval
        }
        self.is_exit_order_active = self.exit_order['profit'] > 0 \
                                    or self.exit_order['loss'] > 0 \
                                    or self.exit_order['trail_offset'] >  0     

    def sltp(
        self,
        profit_long=0,
        profit_short=0,
        stop_long=0,
        stop_short=0,
        eval_tp_next_candle=False,
        round_decimals=None,
        profit_long_callback=None,
        profit_short_callback=None,
        stop_long_callback=None,
        stop_short_callback=None,
        trigger_by='LastPrice',
        split=1,
        interval = 0
    ):
        """
        Simple take profit and stop loss implementation,
        - sends a reduce only stop loss order upon entering a position.
        :param profit_long: profit target value in % for longs
        :param profit_short: profit target value in % for shorts
        :param stop_long: stop loss value for long position in %
        :param stop_short: stop loss value for short position in %
        :param round_decimals: round decimals 
        """
        self.sltp_values = {
            'profit_long': profit_long/100,
            'profit_short': profit_short/100,
            'stop_long': stop_long/100,
            'stop_short': stop_short/100,
            'eval_tp_next_candle': eval_tp_next_candle,
            'profit_long_callback': profit_long_callback,
            'profit_short_callback': profit_short_callback,
            'stop_long_callback': stop_long_callback,
            'stop_short_callback': stop_short_callback,
            'trigger_by': trigger_by,
            'split': split,
            'interval': interval
        } 
        self.is_sltp_active = self.sltp_values['profit_long'] > 0 \
                                or self.sltp_values['profit_short'] > 0 \
                                or self.sltp_values['stop_long'] >  0 \
                                or self.sltp_values['stop_short'] > 0     
        
        if self.quote_rounding == None and round_decimals != None:
            self.quote_rounding = round_decimals

    def get_exit_order(self):
        """
        get profit take and stop loss and trailing settings
        """
        return self.exit_order

    def get_sltp_values(self):
        """
        get values for the simple profit target/stop loss in %
        """
        return self.sltp_values    

    def eval_exit(self):
        """
        evalution of profit target and stop loss and trailing
        """
        if self.get_position_size() == 0:
            return

        unrealised_pnl = self.get_profit()#self.get_position()['unrealisedPnl']

        # trail asset
        if self.get_exit_order()['trail_offset'] > 0 and self.get_trail_price() > 0:
            if self.get_position_size() > 0 and \
                    self.get_market_price() - self.get_exit_order()['trail_offset'] < self.get_trail_price():
                logger.info(f"Loss cut by trailing stop: {self.get_exit_order()['trail_offset']}")
                self.close_all(self.get_exit_order()['trail_callback'],
                                self.get_exit_order()['split'], self.get_exit_order()['interval'])
            elif self.get_position_size() < 0 and \
                    self.get_market_price() + self.get_exit_order()['trail_offset'] > self.get_trail_price():
                logger.info(f"Loss cut by trailing stop: {self.get_exit_order()['trail_offset']}")
                self.close_all(self.get_exit_order()['trail_callback'],
                                self.get_exit_order()['split'], self.get_exit_order()['interval'])

        #stop loss
        if unrealised_pnl < 0 and \
                0 < self.get_exit_order()['loss'] < abs(unrealised_pnl):
            logger.info(f"Loss cut by stop loss: {self.get_exit_order()['loss']}")
            self.close_all(self.get_exit_order()['loss_callback'],
                            self.get_exit_order()['split'], self.get_exit_order()['interval'])

        # profit take
        if unrealised_pnl > 0 and \
                0 < self.get_exit_order()['profit'] < abs(unrealised_pnl):
            logger.info(f"Take profit by stop profit: {self.get_exit_order()['profit']}")
            self.close_all(self.get_exit_order()['profit_callback'],
                            self.get_exit_order()['split'], self.get_exit_order()['interval'])

    def eval_sltp(self):
        """
        Simple take profit and stop loss implementation
        - sends a reduce only stop loss order upon entering a position.
        - requires setting values with sltp() prior      
        """
        pos_size = float(self.get_position_size())
        if pos_size == 0:
            return

        is_tp_full_size = False 
        is_sl_full_size = False       

        # tp
        tp_order = self.get_open_order('TP')           

        if tp_order is not None:
            tp_id = tp_order['orderLinkId'] if self.spot or self.pair.endswith('PERP') else tp_order['order_link_id']    
            origQty = self.get_open_order_qty('TP')#float(tp_order['origQty'])
            is_tp_full_size = origQty == abs(pos_size) if True else False
            #pos_size =  pos_size - origQty                 
        
        tp_percent_long = self.get_sltp_values()['profit_long']
        tp_percent_short = self.get_sltp_values()['profit_short']   

        # sl
        sl_order = self.get_open_order('SL')      
        if sl_order is not None:
            sl_id = sl_order['orderLinkId'] if self.spot or self.pair.endswith('PERP') else sl_order['order_link_id']    
            origQty =  self.get_open_order_qty('SL')#float(sl_order['origQty'])
            orig_side = sl_order['side'] == "Buy" if True else False            
            if (orig_side and pos_size > 0) or (not orig_side and pos_size < 0):                
                self.cancel(id=sl_id)
            if orig_side == False:
                origQty = -origQty            
            is_sl_full_size = origQty == -pos_size if True else False     

        sl_percent_long = self.get_sltp_values()['stop_long']
        sl_percent_short = self.get_sltp_values()['stop_short']

        avg_entry = self.get_position_avg_price()

        # tp execution logic                
        if tp_percent_long > 0 and is_tp_full_size == False:
            if pos_size > 0:                
                tp_price_long = round(avg_entry +(avg_entry*tp_percent_long), self.quote_rounding) 
                if tp_order is not None:
                    #time.sleep(0.05)                                         
                    self.cancel(id=tp_id)
                    #time.sleep(0.05)
                    self.order("TP", False, abs(pos_size), limit=tp_price_long, reduce_only=True,
                                callback=self.get_sltp_values()['profit_long_callback'],
                                trigger_by=self.get_sltp_values()['trigger_by'], 
                                split=self.get_sltp_values()['split'],
                                interval=self.get_sltp_values()['interval'])
                else:               
                    self.order("TP", False, abs(pos_size), limit=tp_price_long, reduce_only=True,
                               callback=self.get_sltp_values()['profit_long_callback'],
                               trigger_by=self.get_sltp_values()['trigger_by'],
                               split=self.get_sltp_values()['split'],
                               interval=self.get_sltp_values()['interval'])
        if tp_percent_short > 0 and is_tp_full_size == False:
            if pos_size < 0:                
                tp_price_short = round(avg_entry -(avg_entry*tp_percent_short), self.quote_rounding)
                if tp_order is not None:
                    #time.sleep(0.05)                                                        
                    self.cancel(id=tp_id)
                    #time.sleep(0.05)
                    self.order("TP", True, abs(pos_size), limit=tp_price_short, reduce_only=True,
                               callback=self.get_sltp_values()['profit_short_callback'], 
                               trigger_by=self.get_sltp_values()['trigger_by'], 
                               split=self.get_sltp_values()['split'], 
                               interval=self.get_sltp_values()['interval'])
                else:
                    self.order("TP", True, abs(pos_size), limit=tp_price_short, reduce_only=True,
                               callback=self.get_sltp_values()['profit_short_callback'], 
                               trigger_by=self.get_sltp_values()['trigger_by'],
                               split=self.get_sltp_values()['split'],
                               interval=self.get_sltp_values()['interval'])    

        # sl execution logic
        if sl_percent_long > 0 and is_sl_full_size == False:
            if pos_size > 0:
                sl_price_long = round(avg_entry - (avg_entry*sl_percent_long), self.quote_rounding)
                if sl_order is not None:
                    #time.sleep(0.05)                                    
                    self.cancel(id=sl_id)
                    #time.sleep(0.05)
                    self.order("SL", False, abs(pos_size), stop=sl_price_long, reduce_only=True,
                                callback=self.get_sltp_values()['stop_long_callback'], 
                                trigger_by=self.get_sltp_values()['trigger_by'],
                                split=self.get_sltp_values()['split'],
                                interval=self.get_sltp_values()['interval'])
                else:  
                    self.order("SL", False, abs(pos_size), stop=sl_price_long, reduce_only=True,
                                callback=self.get_sltp_values()['stop_long_callback'],
                                trigger_by=self.get_sltp_values()['trigger_by'],
                                split=self.get_sltp_values()['split'], 
                                interval=self.get_sltp_values()['interval'])
        if sl_percent_short > 0 and is_sl_full_size == False:
            if pos_size < 0:
                sl_price_short = round(avg_entry + (avg_entry*sl_percent_short), self.quote_rounding)
                if sl_order is not None: 
                    #time.sleep(0.05)                                         
                    self.cancel(id=sl_id)
                    #time.sleep(0.05)
                    self.order("SL", True, abs(pos_size), stop=sl_price_short, reduce_only=True,
                                callback=self.get_sltp_values()['stop_short_callback'], 
                                trigger_by=self.get_sltp_values()['trigger_by'],
                                split=self.get_sltp_values()['split'], 
                                interval=self.get_sltp_values()['interval']) 
                else:  
                    self.order("SL", True, abs(pos_size), stop=sl_price_short, reduce_only=True,
                                callback=self.get_sltp_values()['stop_short_callback'], 
                                trigger_by=self.get_sltp_values()['trigger_by'],
                                split=self.get_sltp_values()['split'], 
                                interval=self.get_sltp_values()['interval'])       

    def fetch_ohlcv(self, bin_size, start_time, end_time):
        """
        fetch OHLCV data
        :param bin_size: timeframe string
        :param start_time: start time
        :param end_time: end time
        :return:
        """        
        self.__init_client()

        fetch_bin_size = allowed_range[bin_size][0]
        left_time = start_time
        right_time = end_time
        data = to_data_frame([])
        bybit_bin_size_converted = bin_size_converter(fetch_bin_size)        

        while True:
            left_time_to_timestamp = int(datetime.timestamp(left_time))
            right_time_to_timestamp = int(datetime.timestamp(right_time))  
            if left_time > right_time:
                break

            logger.info(f"fetching OHLCV data - {left_time}")  

            source = retry(lambda: self.public_client
                           .query_kline(symbol=self.pair,
                                        interval=fetch_bin_size if self.spot else bybit_bin_size_converted['bin_size'],
                                        period=bybit_bin_size_converted['bin_size'], startTime=left_time_to_timestamp, # USDC perps args
                                        from_time=left_time_to_timestamp, limit=1000 if self.spot else 200))
                                                                      
            if len(source) == 0:
                break

            source_to_object_list =[]
           
            for s in source:
                timestamp = s[0] if self.spot else s['openTime'] if self.pair.endswith('PERP') else s['open_time']
                source_to_object_list.append({                        
                    "timestamp" : (datetime.fromtimestamp(int(timestamp / 1000 if len(str(timestamp)) == 13 else timestamp)) 
                        + timedelta(seconds= + bybit_bin_size_converted['seconds']) - timedelta(seconds=0.01)).astimezone(UTC),
                    "high" : float(s[1 if self.spot else 'high']),
                    "low" : float(s[2 if self.spot else 'low']),
                    "open" : float(s[3 if self.spot else 'open']),
                    "close" : float(s[4 if self.spot else 'close']),
                    "volume" : float(s[5 if self.spot else 'volume'])
                    })

            source = to_data_frame(source_to_object_list)
    
            data = pd.concat([data, source])#.dropna()            

            if right_time > source.iloc[-1].name + delta(fetch_bin_size):
                left_time = source.iloc[-1].name + delta(fetch_bin_size)
                time.sleep(2)
            else:
                break      
        
        return resample(data, bin_size)    
    
    def security(self, bin_size, data=None):
        """
        Recalculate and obtain data of a timeframe higher than the current timeframe
        without looking into the future that would cause undesired effects.
        """     
        if data == None: # minute count of a timeframe for sorting when sorting is needed   
            timeframe_list = [allowed_range_minute_granularity[t][3] for t in self.bin_size] 
            timeframe_list.sort(reverse=True)
            t = find_timeframe_string(timeframe_list[-1])     
            data = self.timeframe_data[t]      
            
        return resample(data, bin_size)[:-1]
    
    def __update_ohlcv(self, action, new_data):
        """
        get and update OHLCV data and execute the strategy
        """         
        if self.timeframe_data is None:
            self.timeframe_data = {}            
            for t in self.bin_size:                              
                end_time = datetime.now(timezone.utc)
                start_time = end_time - self.ohlcv_len * delta(t)
                self.timeframe_data[t] = self.fetch_ohlcv(t, start_time, end_time)
                self.timeframe_info[t] = {
                            "allowed_range": allowed_range_minute_granularity[t][0] 
                                                if self.minute_granularity else allowed_range[t][0], 
                            "ohlcv": self.timeframe_data[t][:-1], # Dataframe with closed candles                                                   
                            "last_action_time": None,#self.timeframe_data[t].iloc[-1].name, # Last strategy execution time
                            "last_candle": self.timeframe_data[t].iloc[-2].values,  # Store last complete candle
                            "partial_candle": self.timeframe_data[t].iloc[-1].values  # Store incomplete candle
                            }
                # The last candle is an incomplete candle with timestamp in future                
                if self.timeframe_data[t].iloc[-1].name > end_time:                    
                    last_candle = self.timeframe_data[t].iloc[-1].values # Store last candle
                    self.timeframe_data[t] = self.timeframe_data[t][:-1] # Exclude last candle
                    self.timeframe_data[t].loc[end_time.replace(microsecond=0)] = last_candle #set last candle to end_time

                logger.info(f"Initial Buffer Fill - Last Candle: {self.timeframe_data[t].iloc[-1].name}")   
        #logger.info(f"timeframe_data: {self.timeframe_data}") 

        # Timeframes to be updated
        timeframes_to_update = [allowed_range_minute_granularity[t][3] if self.timeframes_sorted != None 
                                else t for t in self.timeframe_info if self.timeframe_info[t]['allowed_range'] == action]
        # Sorting timeframes that will be updated
        if self.timeframes_sorted == True:
            timeframes_to_update.sort(reverse=True)
        if self.timeframes_sorted == False:
            timeframes_to_update.sort(reverse=False)
        #logger.info(f"timefeames to update: {timeframes_to_update}")        

        for t in timeframes_to_update:
            # Find timeframe string based on its minute count value
            if self.timeframes_sorted != None:             
                t = find_timeframe_string(t)               
                    
            # replace latest candle if timestamp is same or append
            if self.timeframe_data[t].iloc[-1].name == new_data.iloc[0].name:
                self.timeframe_data[t] = pd.concat([self.timeframe_data[t][:-1], new_data])
            else:
                self.timeframe_data[t] = pd.concat([self.timeframe_data[t], new_data])      
            #self.timeframe_data[t] = pd.concat([self.timeframe_data[t], new_data]) 

            # exclude current candle data and store partial candle data
            re_sample_data = resample(self.timeframe_data[t], 
                                      t, 
                                      minute_granularity=True if self.minute_granularity else False)
            self.timeframe_info[t]['partial_candle'] = re_sample_data.iloc[-1].values # store partial candle data
            re_sample_data =re_sample_data[:-1].dropna()  # exclude current candle data

            #logger.info(f"{self.timeframe_info[t]['last_action_time']} : {self.timeframe_data[t].iloc[-1].name} : {re_sample_data.iloc[-1].name}")  

            if self.call_strat_on_start:
                if self.timeframe_info[t]["last_action_time"] is not None and \
                self.timeframe_info[t]["last_action_time"] == re_sample_data.iloc[-1].name:
                    continue
            else:   
                if self.timeframe_info[t]["last_action_time"] is None:
                    self.timeframe_info[t]["last_action_time"] = re_sample_data.iloc[-1].name
                    
                if self.timeframe_info[t]["last_action_time"] == re_sample_data.iloc[-1].name:
                    continue

            # The last candle in the buffer needs to be preserved 
            # while resetting the buffer as it may be incomlete
            # or contains latest data from WS
            self.timeframe_data[t] = pd.concat([re_sample_data.iloc[-1 * self.ohlcv_len:, :], 
                                                self.timeframe_data[t].iloc[[-1]]])
            #store ohlcv dataframe to timeframe_info dictionary
            self.timeframe_info[t]["ohlcv"] = re_sample_data
            #logger.info(f"Buffer Right Edge: {self.data.iloc[-1]}")
            
            open = re_sample_data['open'].values
            close = re_sample_data['close'].values
            high = re_sample_data['high'].values
            low = re_sample_data['low'].values
            volume = re_sample_data['volume'].values 
                                        
            try:
                if self.strategy is not None:   
                    self.timestamp = re_sample_data.iloc[-1].name.isoformat()           
                    self.strategy(t, open, close, high, low, volume)              
                self.timeframe_info[t]['last_action_time'] = re_sample_data.iloc[-1].name
            except FatalError as e:
                # Fatal error
                logger.error(f"Fatal error. {e}")
                logger.error(traceback.format_exc())

                notify(f"Fatal error occurred. Stopping Bot. {e}")
                notify(traceback.format_exc())
                self.stop()
            except Exception as e:
                logger.error(f"An error occurred. {e}")
                logger.error(traceback.format_exc())

    def __on_update_wallet(self, action, wallet):
        """
        update wallet
        """
        self.wallet = {**self.wallet, **wallet}       
        
    def __on_update_instrument(self, action, instrument):
        """
        Update instrument
        """    
        if action not in self.instrument or len(self.instrument) == 0:
            self.instrument[action] = instrument
        
        self.instrument[action].update(instrument)
        #logger.info(f"self.instrument : {self.instrument}")
        
        self.market_price = float(instrument['lastPrice']) if 'lastPrice' in instrument \
                            else float(instrument['c']) if 'c' in instrument else 0  
        
        if self.position_size == None or self.position_size == 0:
                return        
        # trail price update
        if self.position_size > 0 and \
                self.market_price > self.get_trail_price():
            self.set_trail_price(self.market_price)
        if self.position_size < 0 and \
                self.market_price < self.get_trail_price():
            self.set_trail_price(self.market_price)
        #Get PnL calculation in %
        if not self.spot:
            self.pnl = self.get_pnl() 

    def __on_update_fills(self, action, fills):
        """
        Update fills of orders
        """
        self.last_fill = fills
        #self.eval_sltp()    
        #pos_size = self.get_position_size(force_api_call=True)
        #logger.info(f"position size: {pos_size}")
        self.margin = None
        self.get_balance()
        
        message = f"""========= FILLS =============
                           {fills} 
                      ============================="""
        #logger.info(f"{message}")
        notify(message)
    
    def __on_update_order(self, action, orders):
        """
        Update order status        
        """
        self.order_update.append(orders)      
        orders = [o for o in orders if o['s' if self.spot else 'symbol'] == self.pair]
        if len(orders) == 0:
            return
        for o in orders:
            id = o['c' if self.spot else 'orderLinkId']
            side = o['S' if self.spot else 'side']      
            type = o['o' if self.spot else 'orderType']
            status = o['X' if self.spot else 'orderStatus']
            time_in_force = o['f' if self.spot else 'timeInForce']
            qty = float(o['q' if self.spot else 'qty'])
            filled_qty =float(o['z' if self.spot else 'cumExecQty'])
            status = o['X' if self.spot else 'orderStatus']
            limit = float(o['p' if self.spot else 'price'])
            stop = None if self.spot else o['triggerPrice']
            last_fill = None if self.pair.endswith('PERP') else o['l' if self.spot else 'lastExecQty']
            rejec_reason = None if self.spot or self.pair.endswith('PERP') else o['rejectReason']
            APprice = None if self.spot else o['triggerPrice']

            shared_msg = (f"                                 ID     : {id}\n"
                          f"                                 Type   : {type}\n"
                          f"                                 Side   : {side}\n"
                          f"                                 RejecR.: {rejec_reason}\n"
                          f"                                 TIF    : {time_in_force}\n"
                          f"                                 Qty    : {qty}\n"
                          f"                                 LstFill: {last_fill}\n"
                          f"                                 Filled : {filled_qty}\n"
                          f"                                 Limit  : {limit}\n"
                          f"                                 Stop   : {stop}\n"
                          f"                                 APrice : {APprice}")

            if status.upper() in ["CANCELLED", "EXPIRED"] and self.order_update_log:
                logger.info(f"========= Order Update ===============")              
                logger.info(f"Status : {status}\n{shared_msg}")                  
                logger.info(f"======================================")             
                #If stop price is set for a GTC Order and filled quanitity is 0 then EXPIRED means TRIGGERED
                if(float(0 if self.spot else o['triggerPrice']) > 0 \
                   and (o['f' if self.spot else 'timeInForce'] == "GTC" or "GoodTillCancel") \
                   and float(o['z' if self.spot else 'cumExecQty']) == 0 \
                   and o['X' if self.spot else 'orderStatus'] == "EXPIRED"):
                    logger.info(f"========= Order Update ===============")          
                    logger.info(f"Status : TRIGGERED\n{shared_msg}")
                    logger.info(f"{shared_msg}")
                    logger.info(f"======================================")

                self.callbacks.pop(o['c' if self.spot else 'orderLinkId'], None) # Removes the respective order callback

            #only after order if completely filled
            elif self.order_update_log and qty == filled_qty and status.upper() != "CANCELLED": 
                logger.info(f"========= Order Fully Filled =========")      
                logger.info(f"Status : {status}\n{shared_msg}")                
                logger.info(f"======================================")      
                
                # Call the respective order callback       
                if len(self.limit_chaser_ord[side]) > 0 \
                    and id == self.limit_chaser_ord[side]['ID'] \
                    and 'callback' in self.limit_chaser_ord[side]:
                    callback = self.limit_chaser_ord[side].pop('callback', None)  # Removes the respective order callback and returns it   
                    self.limit_chaser_ord[side]['active_counter'] = 0
                else:
                    callback = self.callbacks.pop(o['c' if self.spot else 'orderLinkId'], None)  # Removes the respective order callback and returns it
                if callable(callback):
                    callback()
            else:
                logger.info(f"========= Order Update ===============")           
                logger.info(f"Status : {status}\n{shared_msg}")                  
                logger.info(f"======================================")             

        if len(self.limit_chaser_ord[side]) > 0 and id == self.limit_chaser_ord[side]['ID']:           
            is_active = status.upper() == "CANCELLED"
            is_amend_active = status != 'Filled' \
                                    and status.upper() != "CANCELLED"
       
            limit_chaser_ord = {'ID' : id,
                                'Side' : side,
                                'Status' : status,
                                'Qty' : qty,
                                'Filled' : filled_qty,
                                'Limit': limit,
                                'is_active':  is_active,
                                'is_amend_active': is_amend_active}
            
            if not self.limit_chaser_ord[side]['is_amend_active'] and is_active: # if not flipping from amending to active
                self.limit_chaser_ord[side]['active_counter'] += 1  
            if is_amend_active:
                self.limit_chaser_ord[side]['chase_counter'] += 1 
            self.limit_chaser_ord[side].update(limit_chaser_ord) # Updating chaser order dict

        # Evaluation of profit and loss
        if self.is_exit_order_active:
            self.eval_exit()
        if self.is_sltp_active:
            self.eval_sltp()    
        
    def __on_update_position(self, action, position):
        """
        Update position
        """    
        logger.info(f"{position}")
        if len(position) > 0:
            position = [p for p in position if p["symbol"].startswith(self.pair)]   
            if len(position) == 0:
                # logger.info(f"Some other pair was traded!")
                return
        else:
            return         
            
        # Was the position size changed?
        is_update_pos_size = self.get_position_size() != float(position[0]['size'])        

        # Reset trail to current price if position size changes
        if is_update_pos_size and float(position[0]['size']) != 0:
            self.set_trail_price(self.market_price)

        if is_update_pos_size:
            quote_asset_str = self.base_asset if self.pair.endswith('USD') and not self.spot else self.quote_asset 
            logger.info(f"Updated Position\n"
                        f"Price(entryPrice): {self.position[0]['entryPrice']} => {position[0]['entryPrice']}\n"
                        f"Qty(size): {self.position[0]['size']} => {position[0]['size']}\n"
                        f"liqPrice: {self.position[0]['liqPrice']} => {position[0]['liqPrice']}\n"
                        f"Balance: {self.get_balance()} {quote_asset_str}")
            notify(f"Updated Position\n"
                        f"Price(entryPrice): {self.position[0]['entryPrice']} => {position[0]['entryPrice']}\n"
                        f"Qty(size): {self.position[0]['size']} => {position[0]['size']}\n"
                        f"liqPrice: {self.position[0]['liqPrice']} => {position[0]['liqPrice']}\n"
                        f"Balance: {self.get_balance()} {quote_asset_str}")       
        
        self.position[0].update(position[0])
       
        self.position_size = float(self.position[0]['size'])
        self.entry_price = float(self.position[0]['entryPrice'])        
    
        # Evaluation of profit and loss, calling stop loss and take profit functions
        #self.eval_exit()
        #self.eval_sltp()

    def __on_update_bookticker(self, action, bookticker):
        """
        update best bid and best ask price and quantity
        """ 
        if not self.spot:
            self.bookticker = {k:v for k,v in bookticker.items() if (k=='a' or k=='b') and len(v) > 0}       
        else:
            self.bookticker.update(bookticker)

        if ('bp' in  self.bookticker and 'ap'in self.bookticker) \
            or ('b' in self.bookticker and 'a' in self.bookticker):
            self.best_bid_price = float(self.bookticker['bp']) if self.spot else float(self.bookticker['b'][0][0])
            self.best_ask_price = float(self.bookticker['ap']) if self.spot else float(self.bookticker['a'][0][0])
            self.bid_quantity_L1 = float(self.bookticker['bq']) if self.spot else float(self.bookticker['b'][0][1])          
            self.ask_quantity_L1 = float(self.bookticker['aq']) if self.spot else  float(self.bookticker['a'][0][1])
        #logger.info(f"best bid: {self.best_bid_price}          best_ask: {self.best_ask_price}           bq_L1: {self.bid_quantity_L1}           aq_L1: {self.ask_quantity_L1}")

    def on_update(self, bin_size, strategy):
        """
        Register the strategy function
        bind functions with webosocket data streams        
        :param strategy: strategy
        """       
        self.bin_size = bin_size
        self.strategy = strategy
        logger.info(f"pair: {self.pair}")  
        logger.info(f"timeframes: {bin_size}")    

        if self.is_running:
            self.ws = BybitWs(account=self.account, pair=self.pair, spot=self.spot, test=self.demo)

            #if len(self.bin_size) > 1:   
                #self.minute_granularity=True  

            #if self.minute_granularity==True and '1m' not in self.bin_size:
                #self.bin_size.append('1m')      

            #self.ws.bind('1m' if self.minute_granularity else allowed_range[bin_size[0]][0] \
                        #, self.__update_ohlcv)     

            if len(self.bin_size) > 0: 
                for t in self.bin_size:                                        
                    self.ws.bind(
                        allowed_range_minute_granularity[t][0] if self.minute_granularity else allowed_range[t][0],
                        self.__update_ohlcv
                        )      
            #self.ws.bind("trade", self.__update_ohlcv) # tick data
            self.ws.bind('instrument', self.__on_update_instrument)
            self.ws.bind('wallet', self.__on_update_wallet)
            self.ws.bind('position', self.__on_update_position)
            self.ws.bind('bookticker', self.__on_update_bookticker)
            self.ws.bind('execution', self.__on_update_fills)
            self.ws.bind('order', self.__on_update_order)
            # TODO orderbook
            # self.ob = OrderBook(self.ws)        

    def stop(self):
        """
        Stop the crawler
        """
        self.is_running = False
        self.ws.close()

    def show_result(self):
        """
        Show results
        """
        pass

    def plot(self, name, value, color, overlay=True):
        """
        Draw the graph
        """
        pass