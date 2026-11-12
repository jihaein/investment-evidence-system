
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt


def analyze_cash_exposure(
    hold_value: pd.Series,
    gradual_value: pd.Series,
    gradual_cash: pd.Series,
):
    """
    Compare buy-and-hold (무손절) against gradual selling (서서히).

    All inputs:
        DatetimeIndex
        Daily values in KRW
        Same initial capital and evaluation dates
    """

    df = pd.concat(
        [
            hold_value.rename("hold_value"),
            gradual_value.rename("gradual_value"),
            gradual_cash.rename("cash"),
        ],
        axis=1,
        join="inner",
    ).sort_index()

    if df.empty or df.isna().any().any():
        raise ValueError("Missing or misaligned portfolio data.")

    if not df.index.is_unique:
        raise ValueError("Duplicate dates found.")

    if (df["hold_value"] <= 0).any():
        raise ValueError("Invalid buy-and-hold portfolio value.")

    if (df["gradual_value"] <= 0).any():
        raise ValueError("Invalid gradual portfolio value.")

    if (df["cash"] < -1e-6).any():
        raise ValueError("Unexpected negative cash balance.")

    if (df["cash"] > df["gradual_value"] + 1e-6).any():
        raise ValueError("Cash exceeds total portfolio value.")

    # Portfolio weights
    df["cash_weight"] = df["cash"] / df["gradual_value"]
    df["invested_weight"] = 1 - df["cash_weight"]

    # Cumulative returns, normalized to the first common date
    df["hold_cum"] = df["hold_value"] / df["hold_value"].iloc[0] - 1
    df["gradual_cum"] = (
        df["gradual_value"] / df["gradual_value"].iloc[0] - 1
    )

    # Positive means buy-and-hold is ahead
    df["return_gap_pp"] = (
        df["hold_cum"] - df["gradual_cum"]
    ) * 100

    # Daily returns
    df["hold_return"] = df["hold_value"].pct_change(
        fill_method=None
    )
    df["gradual_return"] = df["gradual_value"].pct_change(
        fill_method=None
    )

    # Diagnostic proxy: cash weight at start of each day
    # times buy-and-hold return during that day.
    # This is NOT exact realized cash drag.
    df["cash_drag_proxy"] = (
        df["cash_weight"].shift(1) * df["hold_return"]
    )

    print("\n=== CASH EXPOSURE ANALYSIS ===")
    print(f"Average cash exposure: {df['cash_weight'].mean():.2%}")
    print(f"Maximum cash exposure: {df['cash_weight'].max():.2%}")
    print(f"Final cash exposure:   {df['cash_weight'].iloc[-1]:.2%}")
    print(f"Final return gap:      {df['return_gap_pp'].iloc[-1]:+.2f} pp")
    print(
        "Cash drag proxy (sum): "
        f"{df['cash_drag_proxy'].sum():+.2%}"
    )

    return df


def plot_cash_analysis(df):
    fig, axes = plt.subplots(3, 1, figsize=(12, 11), sharex=True)

    axes[0].plot(
        df.index, df["hold_cum"] * 100,
        label="Buy and Hold", color="tab:blue"
    )
    axes[0].plot(
        df.index, df["gradual_cum"] * 100,
        label="Gradual Sell", color="tab:green"
    )
    axes[0].set_ylabel("Cumulative return (%)")
    axes[0].legend()
    axes[0].grid(alpha=0.3)

    axes[1].fill_between(
        df.index, 0, df["cash_weight"] * 100,
        color="tab:orange", alpha=0.4
    )
    axes[1].set_ylabel("Cash exposure (%)")
    axes[1].grid(alpha=0.3)

    axes[2].plot(
        df.index, df["return_gap_pp"],
        color="tab:red"
    )
    axes[2].axhline(0, color="black", linestyle="--")
    axes[2].set_ylabel("Return gap (pp)")
    axes[2].set_xlabel("Date")
    axes[2].grid(alpha=0.3)

    plt.tight_layout()
    plt.show()
