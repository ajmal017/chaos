import aiohttp
import asyncio
import async_timeout
import json
import os
import boto3
import logging
from utils import Connection
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Key
import functools
from functools import reduce


class Money(object):
    def __init__(self, amount, ccy):
        self.Ccy = ccy
        self.Amount = amount


class StoreManager(object):
    def __init__(self, logger, loop=None):
        self.__timeout = 10
        self.__logger = logger
        self.__loop = loop if loop is not None else asyncio.get_event_loop()

    @Connection.ioreliable
    async def GetSecurities(self, securities):
        try:
            self.__logger.info('Calling securities query ...')
            pairs = list(map(lambda x: Key('Symbol').eq(x[0]) & Key('Broker').eq(x[1]), securities))
            keyCondition = reduce(lambda x, y: x | y, pairs) if len(pairs) > 1 else pairs[0]

            with async_timeout.timeout(self.__timeout):
                response = await self.__loop.run_in_executor(None,
                                                             functools.partial(self.__Securities.scan,
                                                                               FilterExpression=keyCondition))
                return response['Items']

        except ClientError as e:
            self.__logger.error(e.response['Error']['Message'])
            return None
        except Exception as e:
            self.__logger.error(e)
            return None

    async def __aenter__(self):
        db = boto3.resource('dynamodb', region_name='us-east-1')
        self.__Securities = db.Table('Securities')
        self.__logger.info('StoreManager created')
        return self

    async def __aexit__(self, *args, **kwargs):
        self.__logger.info('StoreManager destroyed')


class IGClient:
    """IG client."""

    def __init__(self, identifier, password, url, key, logger, loop=None):
        self.__timeout = 10
        self.__logger = logger
        self.__id = identifier
        self.__password = password
        self.__url = url
        self.__key = key
        self.__tokens = None
        self.__loop = loop if loop is not None else asyncio.get_event_loop()

    @Connection.ioreliable
    async def Logout(self):
        try:
            url = '%s/%s' % (self.__url, 'session')
            with async_timeout.timeout(self.__timeout):
                self.__logger.info('Calling Logout ...')
                response = await self.__connection.delete(url=url, headers=self.__tokens)
                self.__logger.info('Logout Response Code: {}'.format(response.status))
                return True
        except Exception as e:
            self.__logger.error('Logout: %s, %s' % (self.__url, e))
            return False

    @Connection.ioreliable
    async def Login(self):
        try:
            url = '%s/%s' % (self.__url, 'session')
            with async_timeout.timeout(self.__timeout):
                authenticationRequest = {
                    'identifier': self.__id,
                    'password': self.__password,
                    'encryptedPassword': None
                }
                self.__logger.info('Calling authenticationRequest ...')
                response = await self.__connection.post(url=url, json=authenticationRequest)
                self.__logger.info('Login Response Code: {}'.format(response.status))
                self.__tokens = {'X-SECURITY-TOKEN': response.headers['X-SECURITY-TOKEN'],
                                 'CST': response.headers['CST']}
                payload = await response.json()
                return payload
        except Exception as e:
            self.__logger.error('Login: %s, %s' % (self.__url, e))
            return None

    async def __aenter__(self):
        connector = aiohttp.TCPConnector(verify_ssl=False)
        self.__session = aiohttp.ClientSession(loop=self.__loop, connector=connector,
                                               headers={'X-IG-API-KEY': self.__key})
        self.__connection = await self.__session.__aenter__()
        self.__logger.info('Session created')
        return self

    async def __aexit__(self, *args, **kwargs):
        await self.__session.__aexit__(*args, **kwargs)
        self.__logger.info('Session destroyed')


class Scheduler:
    def __init__(self, identifier, password, url, key, logger, loop=None):
        self.Timeout = 10
        self.__logger = logger
        self.__id = identifier
        self.__password = password
        self.__url = url
        self.__key = key
        self.__store = None
        self.Balance = None
        self.__client = None
        self.__loop = loop if loop is not None else asyncio.get_event_loop()

    async def __aenter__(self):
        self.__store = StoreManager(self.__logger, self.__loop)
        await self.__store.__aenter__()
        self.__client = IGClient(self.__id, self.__password, self.__url, self.__key, self.__logger, self.__loop)
        self.__connection = await self.__client.__aenter__()
        auth = await self.__connection.Login()
        self.Balance = Money(auth['accountInfo']['available'], auth['currencyIsoCode'])
        self.__logger.info('{}'.format(auth))
        self.__logger.info('Scheduler created')
        return self

    async def __aexit__(self, *args, **kwargs):
        await self.__connection.Logout()
        await self.__client.__aexit__(*args, **kwargs)
        await self.__store.__aexit__(*args, **kwargs)
        self.__logger.info('Scheduler destroyed')

    async def ValidateOrders(self, orders):
        keys = [(x['Symbol']['S'], x['Broker']['S']) for x in orders]
        securities = await self.__store.GetSecurities(keys)
        self.__logger.info('Securities %s' % securities)
        found = [(x['Symbol'], x['Risk']['RiskFactor'], x['Risk']['MaxPosition']) for x in securities
                 if x['TradingEnabled'] is True and x['Broker'] == 'IG']

        pending = [(x['OrderId']['S'], x['Symbol']['S'], x['Order']['M']['Size']['N']) for x
                   in orders if x['Broker']['S'] == 'IG']
        valid = [fOrder + pOrder for fOrder in found for pOrder in pending if fOrder[0] == pOrder[1]]

        invalid = [key for key in keys if key not in map(lambda y: (y[0], 'IG'), found)]
        return valid, invalid

    def BalanceCheck(self, order):
        try:
            symbol, riskFactor, maxPosition, orderId, symbol, size = order
            size = float(size)
            self.__logger.info('symbol {}, riskFactor {}, maxPosition {}, symbol {}, size {}'
                               .format(symbol, riskFactor, maxPosition, symbol, size))
            self.__logger.info('Balance {}, Risk {}'.format(self.Balance.Amount, size/self.Balance.Amount))
            if size/self.Balance.Amount > riskFactor:
                return orderId, False
            if size > maxPosition:
                return orderId, False
            return orderId, True
        except Exception as e:
            self.__logger.error('BalanceCheck Error: %s' % e)
            return 'Error', False

    def SendEmail(self, text):
        pass

    async def SendOrder(self, order):
        return order, 'payload'


async def main(loop, logger, event):
    try:
        url = os.environ['IG_URL']
        key = os.environ['X-IG-API-KEY']
        identifier = os.environ['IDENTIFIER']
        password = os.environ['PASSWORD']

        orders = []
        for record in event['Records']:
            if record['eventName'] == 'INSERT':
                orderId = record['dynamodb']['Keys']['OrderId']['S']
                logger.info('New Order received OrderId: %s', orderId)
                orders.append(record['dynamodb']['NewImage'])
            else:
                logger.info('Not INSERT event is ignored')
        if len(orders) == 0:
            logger.info('No Orders. Event is ignored')
            return

        async with Scheduler(identifier, password, url, key, logger, loop) as scheduler:

            valid, invalid = await scheduler.ValidateOrders(orders)
            if len(valid) == 0:
                scheduler.SendEmail('No Valid Security Definition has been found.')
                return
            logger.info('all validated orders %s' % valid)

            passRisk = [scheduler.BalanceCheck(order) for order in valid if scheduler.BalanceCheck(order)[1]]
            failedRisk = [scheduler.BalanceCheck(order) for order in valid if not scheduler.BalanceCheck(order)[1]]
            if len(passRisk) == 0:
                scheduler.SendEmail('No Security has been accepted by Risk Manager.')
                return
            logger.info('all passRisk orders %s' % passRisk)

            futures = [scheduler.SendOrder(o) for o in passRisk]
            done, _ = await asyncio.wait(futures, timeout=scheduler.Timeout)

            results = []
            for fut in done:
                name, payload = fut.result()
                results.append((name, payload))

            text = 'Orders where definition has not been found, not enabled for trading or not IG order %s\n' % invalid
            text += 'Orders where MaxPosition or RiskFactor in Securities table is exceeded %s\n' % failedRisk
            text += 'The results of the trades sent to the IG REST API %s\n' % results
            scheduler.SendEmail(text)

    except Exception as e:
        logger.error(e)


def lambda_handler(event, context):
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logging.basicConfig(format='%(asctime)s - %(levelname)s - %(threadName)s - %(message)s')

    logger.info('event %s' % event)
    logger.info('context %s' % context)

    if 'IG_URL' not in os.environ or 'X-IG-API-KEY' not in os.environ or 'IDENTIFIER' not in os.environ \
            or 'PASSWORD' not in os.environ:
        logger.error('ENVIRONMENT VARS are not set')
        return json.dumps({'State': 'ERROR'})

    app_loop = asyncio.get_event_loop()
    app_loop.run_until_complete(main(app_loop, logger, event))
    app_loop.close()

    return json.dumps({'State': 'OK'})


if __name__ == '__main__':
    with open("event.json") as json_file:
        test_event = json.load(json_file)
        lambda_handler(test_event, None)
