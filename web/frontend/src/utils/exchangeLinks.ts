export function buildExchangeSymbolUrl(
  symbol: string,
  exchange: string | undefined,
  tradingMode?: string,
): string {
  const ex = String(exchange || "").toUpperCase();
  if (!symbol || !ex) return "";
  const clean = symbol.replace("/", "").toUpperCase();
  const mode = String(tradingMode || "").toLowerCase();
  const isPaper = mode.startsWith("paper");

  if (ex === "BINANCE") {
    const base = isPaper ? "https://demo.binance.com/en/futures" : "https://www.binance.com/en/futures";
    return `${base}/${clean}`;
  }
  if (ex === "BYBIT") {
    const base = isPaper ? "https://testnet.bybit.com/trade/usdt" : "https://www.bybit.com/trade/usdt";
    return `${base}/${clean}`;
  }
  return "";
}
