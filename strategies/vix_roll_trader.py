import boto3
import logging
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Key, Attr
import json
from utils import Connection, DecimalEncoder
from contracts import SecurityDefinition
import datetime
import decimal
from dateutil.relativedelta import relativedelta
from functools import reduce
import uuid
import time
import os


class Side:
    Buy = 'BUY'
    Sell = 'SELL'


class Quote(object):
    def __init__(self, symbol):
        self.Symbol = symbol
        self.Date = None
        self.Close = 0.0


class VixTrader(object):
    def __init__(self, logger):
        self.secDef = SecurityDefinition()
        self.Logger = logger
        db = boto3.resource('dynamodb', region_name='us-east-1')
        self.__QuotesEod = db.Table('Quotes.EOD')
        self.__Securities = db.Table('Securities')
        self.__Orders = db.Table('Orders')
        s3 = boto3.resource('s3')
        debug = os.environ["DEBUG_FOLDER"]
        self.__debug = s3.Bucket(debug)

        self.Logger.info('VixTrader Created')
        self.__FrontFuture = Quote(self.secDef.get_front_month_future('VX'))
        self.__OpenPosition = 0
        self.__MaxRoll = 0.1
        self.__StdSize = 100
        self.__VIX = Quote('VIX')

    def S3Debug(self, line):
        self.__debug.download_file('vix_roll.txt', '/tmp/vix_roll.txt')
        f = open('/tmp/vix_roll.tx', 'w')
        f.write(line)
        f.close()
        self.__debug.upload_file('/tmp/vix_roll.txt', 'vix_roll.txt')

    def BothQuotesArrived(self):
        today = datetime.datetime.today().strftime('%Y%m%d')
        vix = self.GetQuotes(self.__VIX.Symbol, today)
        if len(vix) > 0:
            self.__VIX.Close = vix[0]['Details']['Close']
            self.__VIX.Date = vix[0]['Date']
            self.Logger.info('VIX quote for EOD %s has arrived' % today)
        future = self.GetQuotes(self.__FrontFuture.Symbol, today)
        if len(future) > 0:
            self.__FrontFuture.Close = future[0]['Details']['Close']
            self.__FrontFuture.Date = future[0]['Date']
            self.Logger.info('%s quote for EOD %s has arrived' % (self.__FrontFuture.Symbol, today))
        return len(vix) and len(future)

    def GetCurrentPosition(self):
        trades = filter(lambda x: x['Status'] == 'FILLED' or x['Status'] == 'PART_FILLED',
                        self.GetOrders(self.__FrontFuture.Symbol))

        expiry = SecurityDefinition.get_vix_expiry_date(datetime.datetime.today().date())
        nextMonth = list(map(lambda x: x['Trade'],
                             filter(lambda x: x['Maturity'] == expiry.strftime('%Y%m'), trades)))

        if len(nextMonth) == 0:
            self.Logger.info('No open positions have been found')
            return 0

        long = reduce(lambda x, y: x + y,
                      map(lambda x: x['FilledSize'], filter(lambda x: x['Side'] == 'BUY', nextMonth)), 0)
        short = reduce(lambda x, y: x + y,
                       map(lambda x: x['FilledSize'], filter(lambda x: x['Side'] == 'SELL', nextMonth)), 0)

        return long - short

    def IsExceeded(self, side, quantity, position):
        vix = self.GetSecurities()
        if vix is None or len(vix) == 0:
            self.Logger.error('No VX in security definition table')
            return True
        maxPosition = vix[0]['Risk']['MaxPosition']
        self.Logger.info('MaxPosition is %s' % maxPosition)
        if side == Side.Buy and maxPosition < position + quantity:
            return True
        if side == Side.Sell and maxPosition < abs(position - quantity):
            return True

        return False

    def SendOrder(self, symbol, maturity, side, size, reason):
        try:
            order = {
                "Side": side,
                "Size": decimal.Decimal(str(size)),
                "OrdType": "MARKET"
            }
            trade = {}
            strategy = {
                "Name": "VIX ROLL",
                "Reason": reason
            }

            response = self.__Orders.update_item(
                Key={
                    'OrderId': str(uuid.uuid4().hex),
                    'TransactionTime': str(time.time()),
                },
                UpdateExpression="set #st = :st, #s = :s, #m = :m, #p = :p, #b = :b, #o = :o, #t = :t, #str = :str",
                ExpressionAttributeNames={
                    '#st': 'Status',
                    '#s': 'Symbol',
                    '#m': 'Maturity',
                    '#p': 'ProductType',
                    '#b': 'Broker',
                    '#o': 'Order',
                    '#t': 'Trade',
                    '#str': 'Strategy'
                },
                ExpressionAttributeValues={
                    ':st': 'PENDING',
                    ':s': symbol,
                    ':m': maturity,
                    ':p': 'CFD',
                    ':b': 'IG',
                    ':o': order,
                    ':t': trade,
                    ':str': strategy
                },
                ReturnValues="UPDATED_NEW")

        except ClientError as e:
            self.Logger.error(e.response['Error']['Message'])
        except Exception as e:
            self.Logger.error(e)
        else:
            self.Logger.info('Order Created')
            self.Logger.info(json.dumps(response, indent=4, cls=DecimalEncoder))

    def Run(self, symbol):
        self.Logger.info('Run for symbol %s, FrontFuture %s' % (symbol, self.__FrontFuture.Symbol))
        if symbol != self.__VIX.Symbol and symbol != self.__FrontFuture.Symbol:
            self.Logger.warn('Neither spot or Front Future')
            return

        if not self.BothQuotesArrived():
            self.Logger.warn('Need both spot and future to run the strategy')
            return

        today = datetime.datetime.today().date()
        expiry = self.secDef.get_vix_expiry_date(today)
        self.__OpenPosition = self.GetCurrentPosition()
        if self.__OpenPosition != 0 and today == expiry - relativedelta(days=+1):
            self.Logger.warn('Close any open %s trades one day before the expiry on %s' %
                             (self.__FrontFuture.Symbol, expiry))
            side = Side.Sell if self.__OpenPosition > 0 else Side.Buy
            size = abs(self.__OpenPosition)
            self.SendOrder(symbol=self.__FrontFuture.Symbol, side=side, size=size,
                           maturity=expiry.strftime('%Y%m'), reason='CLOSE')
            return

        days_left = (expiry - today).days
        if days_left <= 0:
            self.Logger.warn('Expiry in the past. Expiry: %s. Today: %s' % (expiry, today))
            return

        roll = (self.__FrontFuture.Close - self.__VIX.Close) / days_left
        self.S3Debug('%s,%s,%s,%s,%s,%s\n'
                     % (today.strftime('%Y%m%d'), self.__FrontFuture.Symbol, self.__FrontFuture.Close,
                        self.__VIX.Close, days_left, roll))
        self.Logger.info('The %s roll on %s with %s days left' % (roll, self.__FrontFuture.Symbol, days_left))

        if abs(roll) >= self.__MaxRoll:
            side = Side.Sell if (self.__FrontFuture.Close - self.__VIX.Close) >= 0 else Side.Buy
            if self.IsExceeded(side=side, quantity=self.__StdSize, position=self.__OpenPosition):
                self.Logger.warn('Exceeded MaxPosition size: %s, pos: %s' % (self.__StdSize, self.__OpenPosition))
                return

            self.SendOrder(symbol=self.__FrontFuture.Symbol, side=side, size=self.__StdSize,
                           maturity=expiry.strftime('%Y%m'), reason='OPEN')

    @Connection.reliable
    def GetSecurities(self):
        try:
            self.Logger.info('Calling securities query ...')
            response = self.__Securities.query(
                KeyConditionExpression=Key('Symbol').eq('VX') & Key('Broker').eq('IG'))
        except ClientError as e:
            self.Logger.error(e.response['Error']['Message'])
            return None
        except Exception as e:
            self.Logger.error(e)
            return None
        else:
            if 'Items' in response:
                return response['Items']

    @Connection.reliable
    def GetOrders(self, symbol):
        try:
            self.Logger.info('Calling orders scan attr: %s' % symbol)
            response = self.__Orders.scan(FilterExpression=Attr('Symbol').eq(symbol))

        except ClientError as e:
            self.Logger.error(e.response['Error']['Message'])
            return None
        except Exception as e:
            self.Logger.error(e)
            return None
        else:
            if 'Items' in response:
                return response['Items']

    @Connection.reliable
    def GetQuotes(self, symbol, date):
        try:
            self.Logger.info('Calling quotes query Date key: %s' % date)
            response = self.__QuotesEod.query(
                KeyConditionExpression=Key('Symbol').eq(symbol) & Key('Date').eq(date)
            )
        except ClientError as e:
            self.Logger.error(e.response['Error']['Message'])
            return None
        except Exception as e:
            self.Logger.error(e)
            return None
        else:
            if 'Items' in response:
                return response['Items']


def main(event, context):
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logging.basicConfig(format='%(asctime)s - %(levelname)s - %(threadName)s - %(message)s')

    logger.info('event %s' % event)
    logger.info('context %s' % context)

    response = {'State': 'OK'}
    try:
        vix = VixTrader(logger)
        for record in event['Records']:
            if record['eventName'] == 'INSERT':
                symbol = record['dynamodb']['Keys']['Symbol']['S']
                logger.info('New Quote received Symbol: %s', symbol)
                vix.Run(symbol)
            else:
                logger.info('Not INSERT event is ignored')

        logger.info('Stop VIX trader')

    except Exception as e:
        logger.error(e)
        response['State'] = 'ERROR'

    return response


def lambda_handler(event, context):
    res = main(event, context)
    return json.dumps(res)


if __name__ == '__main__':
    with open("event.json") as json_file:
        test_event = json.load(json_file, parse_float=DecimalEncoder)
    re = main(test_event, None)
    print(json.dumps(re))
