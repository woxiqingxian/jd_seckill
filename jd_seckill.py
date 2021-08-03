
import sys
import os
import pickle
import json
import time
import requests
import random
import logging
import logging.handlers
import config
from datetime import datetime
from concurrent.futures import ProcessPoolExecutor


# LOG_FILENAME = 'jd_seckill_{}.log'.format(datetime.now().strftime("%Y_%m_%d"))
LOG_FILENAME = 'jd_seckill.log'


cookies_dir_path = "./cookies"
if not os.path.exists(cookies_dir_path):
    os.makedirs(cookies_dir_path)

# 初始化日志
logger = logging.getLogger()
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(process)d-%(threadName)s - %(pathname)s[line:%(lineno)d] - %(levelname)s: %(message)s')
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)
file_handler = logging.handlers.RotatingFileHandler(LOG_FILENAME, maxBytes=10485760, backupCount=5, encoding="utf-8")
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)


def wait_some_time(random_range_min=10, random_range_max=100):
    time.sleep(random.randint(random_range_min, random_range_max) / 1000)

def parse_json(s):
    begin = s.find('{')
    end = s.rfind('}') + 1
    return json.loads(s[begin:end])

def open_image(image_file):
    if os.name == "nt":
        os.system('start ' + image_file)  # for Windows
    else:
        if os.uname()[0] == "Linux":
            if "deepin" in os.uname()[2]:
                os.system("deepin-image-viewer " + image_file)  # for deepin
            else:
                os.system("eog " + image_file)  # for Linux
        else:
            os.system("open " + image_file)  # for Mac

def save_image(resp, image_file):
    with open(image_file, 'wb') as f:
        for chunk in resp.iter_content(chunk_size=1024):
            f.write(chunk)

class SKException(Exception):

    def __init__(self, message):
        super().__init__(message)

        return

class Timer(object):
    def __init__(self, sleep_interval_ms=50):
        # '2018-09-28 22:45:50.000'
        self.buy_time = datetime.strptime(config.GLOBAL_CONFIG['buy_time'], "%Y-%m-%d %H:%M:%S.%f")
        self.buy_time_ms = int(time.mktime(self.buy_time.timetuple()) * 1000.0 + self.buy_time.microsecond / 1000)
        self.ahead_ms = random.choice([0, 0, 50, 50, 100])
        self.script_buy_time_ms = self.buy_time_ms - self.ahead_ms
        self.sleep_interval_ms = sleep_interval_ms
        self.diff_time = self.local_jd_time_diff()

    def jd_time(self):
        """
        从京东服务器获取时间毫秒
        :return:
        """
        url = 'https://api.m.jd.com/client.action?functionId=queryMaterialProducts&client=wh5'
        resp = requests.get(url)
        time = int(json.loads(resp.text)["currentTime2"])
        if resp.status_code != 200:
            logger.error("从京东服务器获取时间出错 状态码 :%d " % (resp.status_code))
        time = int(json.loads(resp.text)["currentTime2"])
        return time

    def local_time(self):
        """
        获取本地毫秒时间
        :return:
        """
        return int(round(time.time() * 1000))

    def local_jd_time_diff(self):
        """
        计算本地与京东服务器时间差
        :return:
        """
        return self.local_time() - self.jd_time()

    def start(self):
        logger.info('正在等待到达抢购时间:{}，脚本提前{}毫秒, 检测本地时间与京东服务器时间误差为【{}】毫秒'.format(self.buy_time, self.ahead_ms, self.diff_time))
        while True:
            # 本地时间减去与京东的时间差，能够将时间误差提升到0.1秒附近
            # 具体精度依赖获取京东服务器时间的网络时间损耗
            if self.local_time() - self.diff_time >= self.script_buy_time_ms:
                logger.info('时间到达，开始执行……')
                break
            else:
                time.sleep(self.sleep_interval_ms/1000)

class SpiderSession(object):
    """
    Session相关操作
    """
    def __init__(self, account_info):
        self.account_info = account_info
        self.cookies_file_path = "%s/%s_cookies" % (cookies_dir_path, account_info['username'])
        self.user_agent = account_info['user_agent']
        self.session = self._init_session()
        self.load_cookies_from_local()

    def _init_session(self):
        session = requests.session()
        session.headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3",
            "Connection": "keep-alive"
        }
        return session

    def get_user_agent(self):
        return self.user_agent

    def _set_cookies(self, cookies):
        return self.session.cookies.update(cookies)

    def load_cookies_from_local(self):
        """
        从本地加载Cookie
        :return:
        """
        if not os.path.exists(self.cookies_file_path):
            return False
        # 如果cookie文件超过3个小时，要求重新登录
        if (time.time() - os.path.getctime(self.cookies_file_path)) > 60*60*3:
            return False
        with open(self.cookies_file_path, 'rb') as f:
            local_cookies = pickle.load(f)
        self._set_cookies(local_cookies)
        return

    def save_cookies_to_local(self):
        """
        保存Cookie到本地
        :param cookie_file_name: 存放Cookie的文件名称
        :return:
        """
        with open(self.cookies_file_path, 'wb') as f:
            pickle.dump(self.session.cookies, f)
        return

class QrLogin(object):
    """
    扫码登录
    """
    def __init__(self, account_info):
        """
        初始化扫码登录
        大致流程：
            1、访问登录二维码页面，获取Token
            2、使用Token获取票据
            3、校验票据
        """
        self.account_info = account_info
        self.qrcode_img_file = '%s/%s_qr_code.png' % (cookies_dir_path, account_info['username'])
        self.spider_session = SpiderSession(account_info)
        self.is_login = False
        self.refresh_login_status()

    def refresh_login_status(self):
        """
        刷新是否登录状态
        :return:
        """
        self.is_login = self._validate_cookies()

    def _validate_cookies(self):
        """
        验证cookies是否有效（是否登陆）
        通过访问用户订单列表页进行判断：若未登录，将会重定向到登陆页面。
        :return: cookies是否有效 True/False
        """
        url = 'https://order.jd.com/center/list.action'
        payload = {
            'rid': str(int(time.time() * 1000)),
        }
        try:
            resp = self.spider_session.session.get(url=url, params=payload, allow_redirects=False)
            if resp.status_code == requests.codes.OK \
                and "https://passport.jd.com/uc/login?ReturnUrl" not in resp.text:
                return True
        except Exception as e:
            logger.error("验证cookies是否有效发生异常", e)
        return False

    def _get_login_page(self):
        """
        获取PC端登录页面
        :return:
        """
        url = "https://passport.jd.com/new/login.aspx"
        headers = {
            "User-Agent": self.account_info['user_agent'],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3",
            "Connection": "keep-alive"
        }
        page = self.spider_session.session.get(url, headers=headers)
        return page

    def _get_qrcode(self):
        """
        缓存并展示登录二维码
        :return:
        """
        url = 'https://qr.m.jd.com/show'
        payload = {
            'appid': 133,
            'size': 147,
            't': str(int(time.time() * 1000)),
        }
        headers = {
            'User-Agent': self.spider_session.get_user_agent(),
            'Referer': 'https://passport.jd.com/new/login.aspx',
        }
        resp = self.spider_session.session.get(url=url, headers=headers, params=payload)

        if resp.status_code != requests.codes.OK:
            logger.info('获取二维码失败')
            return False

        # 保存图片
        save_image(resp, self.qrcode_img_file)
        # 打开二维码图片
        logger.info('二维码获取成功，请打开京东APP扫描')
        open_image(self.qrcode_img_file)
        return True

    def _get_qrcode_ticket(self):
        """
        通过 token 获取票据
        :return:
        """
        url = 'https://qr.m.jd.com/check'
        payload = {
            'appid': '133',
            'callback': 'jQuery{}'.format(random.randint(1000000, 9999999)),
            'token': self.spider_session.session.cookies.get('wlfstk_smdl'),
            '_': str(int(time.time() * 1000)),
        }
        headers = {
            'User-Agent': self.spider_session.get_user_agent(),
            'Referer': 'https://passport.jd.com/new/login.aspx',
        }
        resp = self.spider_session.session.get(url=url, headers=headers, params=payload)

        if resp.status_code != requests.codes.OK:
            logger.error('获取二维码扫描结果异常')
            return False

        resp_json = parse_json(resp.text)
        if resp_json['code'] != 200:
            logger.info('Code: %s, Message: %s', resp_json['code'], resp_json['msg'])
            return None
        else:
            logger.info('已完成手机客户端确认')
            return resp_json['ticket']

    def _validate_qrcode_ticket(self, ticket):
        """
        通过已获取的票据进行校验
        :param ticket: 已获取的票据
        :return:
        """
        url = 'https://passport.jd.com/uc/qrCodeTicketValidation'
        headers = {
            'User-Agent': self.spider_session.get_user_agent(),
            'Referer': 'https://passport.jd.com/uc/login?ltype=logout',
        }

        resp = self.spider_session.session.get(url=url, headers=headers, params={'t': ticket})
        if resp.status_code != requests.codes.OK:
            return False

        resp_json = json.loads(resp.text)
        if resp_json['returnCode'] == 0:
            return True
        else:
            logger.info(resp_json)
            return False

    def login_by_qrcode(self):
        """
        二维码登陆
        :return:
        """
        if self.is_login:
            logger.info('已经登录成功')
            return

        self._get_login_page()

        # download QR code
        if not self._get_qrcode():
            raise SKException('二维码下载失败')

        # get QR code ticket
        ticket = None
        retry_times = 85
        for _ in range(retry_times):
            ticket = self._get_qrcode_ticket()
            if ticket:
                break
            time.sleep(2)
        else:
            raise SKException('二维码过期，请重新获取扫描')

        # validate QR code ticket
        if not self._validate_qrcode_ticket(ticket):
            raise SKException('二维码信息校验失败')

        self.refresh_login_status()
        self.spider_session.save_cookies_to_local()

        logger.info('二维码登录成功')
        return

class JdSeckill(object):
    def __init__(self, account_info):
        self.account_info = account_info
        self.spider_session = SpiderSession(account_info)
        self.session = self.spider_session.session
        self.user_agent = self.spider_session.user_agent
        self.sku_id = config.GLOBAL_CONFIG['sku_id']
        self.seckill_num = account_info['seckill_num']
        return

    def reserve(self):
        make_reserve_result = False
        while True:
            try:
                make_reserve_result = self.make_reserve()
            except Exception as e:
                make_reserve_result = False
                logger.info('预约发生异常!', e)
            if make_reserve_result:
                break
            wait_some_time()

        return

    def make_reserve(self):
        make_reserve_result = False
        url = 'https://yushou.jd.com/youshouinfo.action?'
        payload = {
            'callback': 'fetchJSON',
            'sku': self.sku_id,
            '_': str(int(time.time() * 1000)),
        }
        headers = {
            'User-Agent': self.user_agent,
            'Referer': 'https://item.jd.com/{}.html'.format(self.sku_id),
        }
        resp = self.session.get(url=url, params=payload, headers=headers)
        resp_json = parse_json(resp.text)
        reserve_url = resp_json.get('url')
        while True:
            try:
                self.session.get(url='https:' + reserve_url)
                logger.info('预约成功，已获得抢购资格 / 您已成功预约过了，无需重复预约')
                make_reserve_result = True
                break
            except Exception as e:
                logger.error('预约失败正在重试...')
        return make_reserve_result

    def seckill_by_proc_pool(self):
        # with ProcessPoolExecutor(config.GLOBAL_CONFIG['work_count']) as pool:
        pool = ProcessPoolExecutor(config.GLOBAL_CONFIG['work_count'])
        for i in range(config.GLOBAL_CONFIG['work_count']):
            pool.submit(self.seckill)
        pool.shutdown(wait=False)
        return

    def seckill(self):
        Timer().start()
        while True:
            if config.GLOBAL_CONFIG['debug']:
                time.sleep(random.randint(1, 5))
                logger.info(self.account_info['username'] + '测试环境，抢购结束')
                break
            try:
                self.request_seckill_url()
                while True:
                    self.request_seckill_checkout_page()
                    self.submit_seckill_order()
            except Exception as e:
                logger.info('[非期望内异常] 抢购发生异常，稍后继续执行！', e)
            wait_some_time(0, 50)

    def request_seckill_url(self):
        """获取商品的抢购链接
        点击"抢购"按钮后，会有两次302跳转，最后到达订单结算页面
        这里返回第一次跳转后的页面url，作为商品的抢购链接
        """
        seckill_url = ""
        url = 'https://itemko.jd.com/itemShowBtn'
        payload = {
            'callback': 'jQuery{}'.format(random.randint(1000000, 9999999)),
            'skuId': self.sku_id,
            'from': 'pc',
            '_': str(int(time.time() * 1000)),
        }
        headers = {
            'User-Agent': self.user_agent,
            'Host': 'itemko.jd.com',
            'Referer': 'https://item.jd.com/{}.html'.format(self.sku_id),
        }
        while True:
            resp = self.session.get(url=url, headers=headers, params=payload)
            resp_json = parse_json(resp.text)
            if resp_json.get('url'):
                # https://divide.jd.com/user_routing?skuId=8654289&sn=c3f4ececd8461f0e4d7267e96a91e0e0&from=pc
                router_url = 'https:' + resp_json.get('url')
                # https://marathon.jd.com/captcha.html?skuId=8654289&sn=c3f4ececd8461f0e4d7267e96a91e0e0&from=pc
                seckill_url = router_url.replace('divide', 'marathon').replace('user_routing', 'captcha.html')
                logger.info("[获取抢购链接] 获取成功: %s", seckill_url)
                break
            else:
                logger.info("[获取抢购链接] 获取失败，稍后自动重试")
                wait_some_time(0, 50)

        logger.info('[获取抢购链接] 访问商品的抢购连接...')
        headers = {
            'User-Agent': self.user_agent,
            'Host': 'marathon.jd.com',
            'Referer': 'https://item.jd.com/{}.html'.format(self.sku_id),
        }
        self.session.get(url=seckill_url, headers=headers, allow_redirects=False)
        return

    def request_seckill_checkout_page(self):
        """访问抢购订单结算页面"""
        logger.info('[结算页面] 访问抢购订单结算页面...')
        url = 'https://marathon.jd.com/seckill/seckill.action'
        payload = {
            'skuId': self.sku_id,
            'num': self.seckill_num,
            'rid': int(time.time())
        }
        headers = {
            'User-Agent': self.user_agent,
            'Host': 'marathon.jd.com',
            'Referer': 'https://item.jd.com/{}.html'.format(self.sku_id),
        }
        self.session.get(url=url, params=payload, headers=headers, allow_redirects=False)

        return

    def _get_seckill_init_info(self):
        """获取秒杀初始化信息（包括：地址，发票，token）
        :return: 初始化信息组成的dict
        """
        logger.info('[抢购参数获取] 获取秒杀初始化信息...')
        url = 'https://marathon.jd.com/seckillnew/orderService/pc/init.action'
        data = {
            'sku': self.sku_id,
            'num': self.seckill_num,
            'isModifyAddress': 'false',
        }
        headers = {
            'User-Agent': self.user_agent,
            'Host': 'marathon.jd.com',
        }
        resp = self.session.post(url=url, data=data, headers=headers)
        logger.info('[抢购参数获取] 参数日志:{}'.format(resp.text))
        resp_json = parse_json(resp.text)
        return resp_json

    def _get_seckill_order_data(self):
        """生成提交抢购订单所需的请求体参数
        :return: 请求体参数组成的dict
        """
        logger.info('[抢购参数拼接] 生成提交抢购订单所需参数...')
        # 获取用户秒杀初始化信息
        seckill_init_info = self._get_seckill_init_info()
        default_address = seckill_init_info['addressList'][0]  # 默认地址dict
        invoice_info = seckill_init_info.get('invoiceInfo', {})  # 默认发票信息dict, 有可能不返回
        token = seckill_init_info['token']
        data = {
            'skuId': self.sku_id,
            'num': self.seckill_num,
            'addressId': default_address['id'],
            'yuShou': 'true',
            'isModifyAddress': 'false',
            'name': default_address['name'],
            'provinceId': default_address['provinceId'],
            'cityId': default_address['cityId'],
            'countyId': default_address['countyId'],
            'townId': default_address['townId'],
            'addressDetail': default_address['addressDetail'],
            'mobile': default_address['mobile'],
            'mobileKey': default_address['mobileKey'],
            'email': default_address.get('email', ''),
            'postCode': '',
            'invoiceTitle': invoice_info.get('invoiceTitle', -1),
            'invoiceCompanyName': '',
            'invoiceContent': invoice_info.get('invoiceContentType', 1),
            'invoiceTaxpayerNO': '',
            'invoiceEmail': '',
            'invoicePhone': invoice_info.get('invoicePhone', ''),
            'invoicePhoneKey': invoice_info.get('invoicePhoneKey', ''),
            'invoice': 'true' if invoice_info else 'false',
            'password': self.account_info['payment_pwd'],
            'codTimeType': 3,
            'paymentType': 4,
            'areaCode': '',
            'overseas': 0,
            'phone': '',
            'eid': self.account_info['eid'],
            'fp': self.account_info['fp'],
            'token': token,
            'pru': ''
        }

        return data

    def submit_seckill_order(self):
        """提交抢购（秒杀）订单
        :return: 抢购结果 True/False
        """
        url = 'https://marathon.jd.com/seckillnew/orderService/pc/submitOrder.action'
        payload = {
            'skuId': self.sku_id,
        }
        try:
            seckill_order_data = self._get_seckill_order_data()
        except Exception as e:
            logger.info('[提交抢购] 抢购失败，无法获取生成订单的基本信息，错误信息:【{}】'.format(str(e)))
            return False

        logger.info('[提交抢购] 提交抢购订单...')
        headers = {
            'User-Agent': self.user_agent,
            'Host': 'marathon.jd.com',
            'Referer': 'https://marathon.jd.com/seckill/seckill.action?skuId={0}&num={1}&rid={2}'.format(self.sku_id, self.seckill_num, int(time.time())),
        }
        resp = self.session.post(
            url=url,
            params=payload,
            data=seckill_order_data,
            headers=headers
        )
        resp_json = None
        try:
            resp_json = parse_json(resp.text)
        except Exception as e:
            logger.info('[提交抢购] 抢购失败，返回信息:{}'.format(resp.text[0: 128]))
            return False
        # 返回信息
        # 抢购失败：
        # {'errorMessage': '很遗憾没有抢到，再接再厉哦。', 'orderId': 0, 'resultCode': 60074, 'skuId': 0, 'success': False}
        # {'errorMessage': '抱歉，您提交过快，请稍后再提交订单！', 'orderId': 0, 'resultCode': 60017, 'skuId': 0, 'success': False}
        # {'errorMessage': '系统正在开小差，请重试~~', 'orderId': 0, 'resultCode': 90013, 'skuId': 0, 'success': False}
        # 抢购成功：
        # {"appUrl":"xxxxx","orderId":820227xxxxx,"pcUrl":"xxxxx","resultCode":0,"skuId":0,"success":true,"totalMoney":"xxxxx"}
        if resp_json.get('success'):
            order_id = resp_json.get('orderId')
            total_money = resp_json.get('totalMoney')
            pay_url = 'https:' + resp_json.get('pcUrl')
            logger.info(
                """
                ========================================================================
                [提交抢购] 抢购成功，
                    订单号:{},
                    总价:{},
                    电脑端付款链接:{}
                ============================================================================
                """.format(order_id, total_money, pay_url))
            return True
        else:
            logger.info('[提交抢购] 抢购失败，返回信息:{}'.format(resp_json))
            return False

def do_user_login():
    for account_info in config.GLOBAL_CONFIG['account_list']:
        QrLogin(account_info).login_by_qrcode()
    return

def do_user_reserve():
    for account_info in config.GLOBAL_CONFIG['account_list']:
        JdSeckill(account_info).reserve()
    return

def do_user_seckill():
    for account_info in config.GLOBAL_CONFIG['account_list']:
        JdSeckill(account_info).seckill_by_proc_pool()
    return

if __name__ == '__main__':
    a = """
功能列表：
 1.检查登录
 2.预约商品
 3.秒杀抢购商品
    """
    print(a)

    choice_function = input('请选择:')
    # 执行功能
    if choice_function == '1':
        do_user_login()
    elif choice_function == '2':
        do_user_login()
        do_user_reserve()
    elif choice_function == '3':
        do_user_login()
        do_user_seckill()
    else:
        sys.exit(1)
