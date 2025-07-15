import logging, os
from tqdm import tqdm
from dotenv import load_dotenv
load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

logging.addLevelName(35, "LLM")
def llm(self, message, *args, **kws):
    """
    记录一条级别为 LLM 的日志消息
    """
    if self.isEnabledFor(35):
        # self._log 是 logging 模块推荐的用于发出日志记录的内部方法
        self._log(35, message, args, **kws)

logging.Logger.llm = llm

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

# 添加自定义的TqdmLoggingHandler用于控制台输出
tqdm_handler = TqdmLoggingHandler()
tqdm_handler.setLevel(logging.INFO)
console_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
tqdm_handler.setFormatter(console_formatter)
logger.addHandler(tqdm_handler)

# 添加FileHandler用于文件输出
file_handler = logging.FileHandler('boss_hire.log', encoding='utf-8')
file_handler.setLevel(logging.WARNING)
file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(file_formatter)
logger.addHandler(file_handler)
