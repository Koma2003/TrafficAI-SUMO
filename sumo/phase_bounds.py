"""Bounded projection of green durations onto a fixed cycle budget."""
import math


def bounded_durations(values, total, minimum, maximum):
    """Keep every green within bounds and preserve the sum (seconds)."""
    values = list(values)
    if not values or not all(math.isfinite(x) for x in [*values, total, minimum, maximum]):
        raise ValueError("Нужны фазы и конечные числовые значения")
    n = len(values)
    if minimum <= 0 or maximum < minimum:
        raise ValueError("Требуется 0 < минимум зелёного <= максимум")
    if not n * minimum - 1e-8 <= total <= n * maximum + 1e-8:
        raise ValueError(
            f"Невозможно сохранить цикл: {n} зелёных фаз по {minimum}–{maximum} с "
            f"требуют суммарно {n * minimum}–{n * maximum} с, доступно {total} с. "
            "Измените границы или исходный цикл."
        )
    lo = minimum - max(values)
    hi = maximum - min(values)
    for _ in range(100):
        shift = (lo + hi) / 2
        if sum(max(minimum, min(maximum, v + shift)) for v in values) < total:
            lo = shift
        else:
            hi = shift
    return [max(minimum, min(maximum, v + (lo + hi) / 2)) for v in values]
