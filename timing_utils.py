"""生产等待间隔的共享抖动，不改变超时或稳定性判定。"""
from random import gauss


def jitter(mu: float, sigma: float | None = None) -> float:
    """按均值的25%加入正态抖动，等待至少为均值的30%。"""
    if sigma is None:
        sigma = mu * 0.25
    return max(mu + gauss(0, sigma), mu * 0.3)


def exponential_backoff(attempt: int, base: float = 0.5, cap: float = 5.0) -> float:
    """带抖动的指数退避；attempt 从 0 开始，返回值不超过 cap。"""
    return min(cap, jitter(base * (2 ** attempt)))
