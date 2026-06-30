def generate_weight_chart(weight_history: list) -> str:
    if not weight_history:
        return "Нет данных для графика"
    lines = []
    min_w = min(w.weight_kg for w in weight_history)
    max_w = max(w.weight_kg for w in weight_history)
    diff = max_w - min_w or 1
    for entry in weight_history[-14:]:
        pct = (entry.weight_kg - min_w) / diff
        bar_len = int(pct * 20)
        bar = "█" * bar_len + "░" * (20 - bar_len)
        date_str = entry.date.strftime("%d.%m") if hasattr(entry.date, "strftime") else str(entry.date)[:5]
        lines.append(f"{date_str} {bar} {entry.weight_kg:.1f}кг")
    return "\n".join(lines)
