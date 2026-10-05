"""Frozen first-pass policy for the expanded shadow-only universe."""

POLICY = {
    "policy_id": "EXPANDED_TECH_ADJACENT_SHADOW_V1",
    "maximum_close_twd": 190.0,
    "minimum_turnover_twd": 30_000_000,
    "maximum_symbols": 400,
    "security_code_pattern": r"^[0-9]{4}$",
    # Electrical machinery (05), green energy (35), and the official
    # electronic/technology categories intentionally remain eligible.  Mixed
    # "other" categories remain observable rather than being guessed away.
    "excluded_twse_industry_codes": [
        "01", "02", "03", "04", "06", "08", "09", "10", "11",
        "12", "14", "15", "16", "17", "18", "21", "22", "23",
        "33", "37", "38",
    ],
    "excluded_industry_names": [
        "水泥工業", "食品工業", "塑膠工業", "紡織纖維", "電器電纜",
        "玻璃陶瓷", "造紙工業", "鋼鐵工業", "橡膠工業", "汽車工業",
        "建材營造", "航運業", "觀光餐旅", "金融業", "金融保險業",
        "貿易百貨", "化學工業", "生技醫療業", "油電燃氣業",
        "農業科技", "運動休閒", "居家生活",
    ],
    "ranking": ["turnover_twd_desc", "stock_id_asc"],
    "benchmark_symbol": "0050",
    "mode": "SHADOW_ONLY_READ_ONLY_QUOTES_AND_POST_SESSION_PAPER",
    "actual_orders": 0,
    "actual_fills": 0,
    "broker_order_calls": 0,
}
