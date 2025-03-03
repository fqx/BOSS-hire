import logging, os
from tqdm import tqdm
from dotenv import load_dotenv
load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

class TqdmLoggingHandler(logging.Handler):
    def __init__(self, level=logging.NOTSET):
        super().__init__(level)
        self._tqdm_instance = None

    def set_tqdm(self, tqdm_instance):
        self._tqdm_instance = tqdm_instance

    def emit(self, record):
        try:
            msg = self.format(record)
            # 如果有活跃的tqdm实例，使用其write方法
            if self._tqdm_instance:
                self._tqdm_instance.write(msg)
            else:
                tqdm.write(msg)
            self.flush()
        except Exception:
            self.handleError(record)

logger = logging.getLogger()
logger.setLevel(LOG_LEVEL)

# 移除所有默认的handler
for handler in logger.handlers[:]:
    logger.removeHandler(handler)

# 添加自定义的TqdmLoggingHandler
handler = TqdmLoggingHandler()
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)