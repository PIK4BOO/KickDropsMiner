"""Retry delay helper with jitter."""
import random


class ExponentialBackoff:
    """Generate capped exponential delays with random jitter."""

    def __init__(self, base=2.0, variance=0.15, shift=0.0, maximum=300.0):
        if base <= 1:
            raise ValueError("base must be greater than 1")
        self.base = float(base)
        self.shift = float(shift)
        self.maximum = float(maximum)
        self.steps = 0
        if isinstance(variance, tuple):
            self.variance_min, self.variance_max = variance
        else:
            self.variance_min = 1 - float(variance)
            self.variance_max = 1 + float(variance)

    def __iter__(self):
        return self

    def __next__(self):
        value = (
            pow(self.base, self.steps)
            * random.uniform(self.variance_min, self.variance_max)
            + self.shift
        )
        if value < self.maximum:
            self.steps += 1
        return min(value, self.maximum)

    def reset(self):
        self.steps = 0
