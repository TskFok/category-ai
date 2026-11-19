from app.core.config import Settings


def estimate_llm_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    settings: Settings,
) -> float:
    """
    Estimate USD cost from configurable per-1M-token rates (Section 8.3 & ADR-10).
    This is an estimate for budgeting/observability, not an official invoice.
    """
    if model == settings.deepseek_analysis_model:
        in_rate = settings.price_analysis_input_per_1m
        out_rate = settings.price_analysis_output_per_1m
    else:
        in_rate = settings.price_extract_input_per_1m
        out_rate = settings.price_extract_output_per_1m

    cost = (input_tokens / 1_000_000.0) * in_rate + (output_tokens / 1_000_000.0) * out_rate
    return round(cost, 6)
