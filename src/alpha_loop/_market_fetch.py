"""Isolated native request; parent enforces a wall-clock timeout."""
import json
import sys


def main():
    import yfinance as yf
    try:
        ticker, start, end = sys.argv[1:]
        frame = yf.Ticker(ticker).history(start=start, end=end, interval="1d", auto_adjust=False,
                                        actions=True, repair=False, keepna=True, timeout=20)
        sys.stdout.buffer.write(frame.to_csv().encode("utf-8"))
        return 0
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__, "error": str(error)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
