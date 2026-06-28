"""
Генерация графиков прогресса: вес, дефицит/профицит, сон.
Возвращает BytesIO для отправки как документ в Telegram.
"""
import io
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime, timedelta

plt.style.use("seaborn-v0_8-darkgrid")


def _fig_to_bytes(fig) -> io.BytesIO:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight")
    buf.seek(0)
    plt.close(fig)
    return buf


def weight_chart(dates: list[datetime], weights: list[float],
                 target: float | None = None) -> io.BytesIO:
    """
    График веса с линией цели.
    dates: список дат, weights: список весов.
    """
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot(dates, weights, "o-", color="#2196F3", linewidth=2, markersize=5, label="Вес")

    if target is not None:
        ax.axhline(y=target, color="#4CAF50", linestyle="--", linewidth=1.5, label=f"Цель: {target}кг")

    ax.set_title("Прогресс веса", fontsize=14, fontweight="bold")
    ax.set_ylabel("кг", fontsize=11)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    fig.autofmt_xdate()
    ax.legend(fontsize=10)
    ax.grid(True, alpha=0.3)

    return _fig_to_bytes(fig)


def deficit_chart(dates: list[datetime], deficits: list[float]) -> io.BytesIO:
    """
    График дефицита/профицита калорий.
    Положительные = дефицит (хорошо для похудения).
    Отрицательные = профицит.
    """
    fig, ax = plt.subplots(figsize=(8, 4))
    colors = ["#4CAF50" if d >= 0 else "#F44336" for d in deficits]
    ax.bar(dates, deficits, color=colors, width=0.7, alpha=0.8)
    ax.axhline(y=0, color="gray", linewidth=0.8)

    avg = sum(deficits) / len(deficits) if deficits else 0
    ax.axhline(y=avg, color="#FF9800", linestyle="--", linewidth=1.5,
               label=f"Среднее: {avg:+.0f} ккал")

    ax.set_title("Дефицит / Профицит калорий", fontsize=14, fontweight="bold")
    ax.set_ylabel("ккал", fontsize=11)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    fig.autofmt_xdate()
    ax.legend(fontsize=10)
    ax.grid(True, alpha=0.3)

    return _fig_to_bytes(fig)


def sleep_chart(dates: list[datetime], durations: list[float],
                target_hours: float = 8.0) -> io.BytesIO:
    """
    График длительности сна с линией цели.
    """
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.bar(dates, durations, color="#7E57C2", width=0.7, alpha=0.8)
    ax.axhline(y=target_hours, color="#FF9800", linestyle="--", linewidth=1.5,
               label=f"Цель: {target_hours}ч")

    avg = sum(durations) / len(durations) if durations else 0
    status = "OK" if avg >= 7 else "мало!"
    ax.set_title(f"Сон (среднее: {avg:.1f}ч) {status}", fontsize=14, fontweight="bold")
    ax.set_ylabel("часов", fontsize=11)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    fig.autofmt_xdate()
    ax.legend(fontsize=10)
    ax.grid(True, alpha=0.3)

    return _fig_to_bytes(fig)


def weekly_summary_chart(
    dates: list[datetime],
    calories: list[float],
    protein: list[float],
    targets_cal: float,
    targets_prot: float,
) -> io.BytesIO:
    """
    Двойной график: калории и белок за неделю с линиями целей.
    """
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10, 4))

    ax1.bar(dates, calories, color="#FF7043", width=0.7, alpha=0.8)
    ax1.axhline(y=targets_cal, color="#2196F3", linestyle="--", label=f"Цель: {targets_cal}")
    ax1.set_title("Калории", fontsize=12, fontweight="bold")
    ax1.set_ylabel("ккал")
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    ax1.legend(fontsize=9)
    ax1.grid(True, alpha=0.3)

    ax2.bar(dates, protein, color="#66BB6A", width=0.7, alpha=0.8)
    ax2.axhline(y=targets_prot, color="#2196F3", linestyle="--", label=f"Цель: {targets_prot}г")
    ax2.set_title("Белок", fontsize=12, fontweight="bold")
    ax2.set_ylabel("г")
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    ax2.legend(fontsize=9)
    ax2.grid(True, alpha=0.3)

    fig.autofmt_xdate()
    fig.suptitle("Неделя", fontsize=14, fontweight="bold", y=1.02)

    return _fig_to_bytes(fig)


def workout_weight_chart(
    weight_dates: list[datetime],
    weights: list[float],
    workout_dates: list[datetime],
    volumes: list[float],
) -> io.BytesIO:
    """
    Двойной график: вес тела (линия) + объём тренировок (бары).
    """
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(8, 6), sharex=False)

    if weight_dates:
        ax1.plot(weight_dates, weights, "o-", color="#2196F3", linewidth=2, markersize=5)
        ax1.set_title("Вес тела", fontsize=12, fontweight="bold")
        ax1.set_ylabel("кг")
        ax1.grid(True, alpha=0.3)
    else:
        ax1.text(0.5, 0.5, "Нет данных", ha="center", va="center", fontsize=12, color="gray")
        ax1.set_title("Вес тела")

    if workout_dates:
        ax2.bar(workout_dates, volumes, color="#1565C0", alpha=0.8)
        ax2.set_title("Объём тренировок", fontsize=12, fontweight="bold")
        ax2.set_ylabel("кг (вес x повторы)")
        ax2.grid(True, alpha=0.3)
    else:
        ax2.text(0.5, 0.5, "Нет данных", ha="center", va="center", fontsize=12, color="gray")
        ax2.set_title("Объём тренировок")

    fig.tight_layout()
    return _fig_to_bytes(fig)
