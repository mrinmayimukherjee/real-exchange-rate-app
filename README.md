# Real exchange rate explorer

A Streamlit web app that computes and charts the real exchange rate (RER) between any two IMF economies, plus the Euro area, using live monthly CPI and exchange-rate data from the IMF.

Built for an MBA International Finance course. It answers one question: compared with its own history, is Country 1's currency strong or weak in real terms against Country 2's?

## What the app shows

- A plain-language headline, for example: "In Aug 2026, the British pound was 8.2% stronger in real terms against the US dollar than its Jan 1976–Aug 2026 average."
- Four summary cards: the latest nominal rate S, the latest real index, the sample average RER*, and the count of valid months.
- An interactive chart of the real index, the nominal rate, or both.
- A data table with CSV download.
- A "How it's calculated" tab with formulas, the IMF series used and a tool to check any month by hand.

## Formulas

The IMF reports exchange rates as local currency per US dollar, E(t). The US rate is 1 by definition.

| Quantity | Formula | Meaning |
|---|---|---|
| Nominal rate | S(t) = E2(t) / E1(t) | Units of Country 2 currency per 1 unit of Country 1 currency |
| Real exchange rate | RER(t) = S(t) × P1(t) / P2(t) | Country 1 goods priced in Country 2 goods |
| Average RER | RER* = mean of RER(t) over valid months in the selected sample | Recomputed whenever the dates change |
| Real index | Real Index(t) = 100 × RER(t) / RER* | 100 = sample average; above 100 = Country 1 currency stronger in real terms |

A month is valid only if E1, E2, P1 and P2 all exist and are positive. Nothing is interpolated; gaps stay gaps.

Example: with Country 1 = United Kingdom and Country 2 = United States, S is in USD per GBP (about 1.2–1.4 in recent years).

## Data sources

All data comes from the IMF SDMX 3.0 API. No API key is needed.

URL pattern:

```
https://api.imf.org/external/sdmx/3.0/data/dataflow/IMF.STA/{DATAFLOW}/~/{KEY}
```

| Input | Dataflow | Series key | Notes |
|---|---|---|---|
| CPI | `CPI` | `{ISO3}.CPI._T.IX.M` | All items, index, monthly |
| CPI fallback | `CPI` | `{ISO3}.HICP._T.IX.M` | Used if the CPI key returns nothing (often the Euro area) |
| Exchange rate | `ER` | `{ISO3}.XDC_USD.PA_RT.M` | Domestic currency per USD, period average, monthly |

- The Euro area uses code `G163`.
- Requests ask for CSV (`Accept: text/csv`) from January 1976 (`c[TIME_PERIOD]=ge:1976-M01`). The app also filters locally in case the server ignores the filter.
- HTTP 404 or an empty response is treated as "no data". Network errors are retried with timeouts and are never cached. Successful downloads are cached for 6 hours.

## Deploy on Streamlit Community Cloud

1. Create a public GitHub repository and add `app.py`, `requirements.txt` and `README.md` at the top level.
2. Go to https://share.streamlit.io and sign in with GitHub.
3. Select **Create app**, then choose to deploy from an existing GitHub repo.
4. Pick your repository and branch, and set the main file path to `app.py`.
5. Open **Advanced settings** and choose Python 3.12.
6. Select **Deploy**. The first build takes a few minutes while packages install.

To update the app later, commit changes to the repo; Streamlit Cloud redeploys automatically.

## Limitations

- **Data gaps.** Coverage varies by country. Some economies have no CPI or exchange-rate series in the IMF datasets, or start well after 1976. Missing months are excluded and reported in the app.
- **CPI base years differ.** Each country's CPI uses its own base year, so the RER level has no economic meaning by itself. The real index removes this by normalizing to the sample average, but the index level depends on the sample you choose.
- **Currency redenominations.** Countries that changed currency (euro adoption in 1999–2023, or redenominations such as Turkey 2005 and Venezuela) may show breaks. The IMF usually expresses history in the current currency unit, but not always consistently. Currency labels in the app use today's currency code.
- **Euro area exchange rate** begins in 1999.
- **High inflation economies** can produce extreme values that dominate the average.
- **Period averages.** Exchange rates are monthly averages, not end-of-month rates. CPIs are not seasonally adjusted.
- **Live API.** The app depends on the IMF API. If IMF changes series keys or the service is down, the app shows an error message instead of crashing.

## Verify the calculations

1. With United Kingdom vs United States, the nominal card should read roughly 1.2–1.4 USD per GBP. In "Check one month by hand", E1 (GBP per USD) is below 1 and E2 = 1, so S = 1 / E1 > 1.
2. Swap the countries with ⇄. S should flip to roughly 0.7–0.8 GBP per USD.
3. In the exported CSV, for any row with `valid = True`: `S = E2 / E1`, `RER = S × P1 / P2`, `RER_star = average of RER over valid rows`, `real_index = 100 × RER / RER_star`.
