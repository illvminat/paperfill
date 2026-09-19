# Источники истины

Утверждения о поведении платформы в этом проекте опираются на источники
ниже. Утверждение без источника помечается как непроверенное.

Хранилище: `~/Documents/refdocs/` (локальное, не копируется в репозиторий).
Версия: Polymarket docs, снимок 2026-09-19; polymarket-client 0.10.0.

## Polymarket — как устроено

| Область | Источник | Отвечает на | Загружен |
|---|---|---|---|
| Комиссии | polymarket/fees.md | формула `fee = C × feeRate × p × (1 − p)`, ставки по категориям, только тейкер платит, округление до 5 знаков | 2026-09-19 |
| Комиссии рынка | polymarket/market-details.md, раздел Trading Fees | `feeSchedule`: rate, exponent, takerOnly, rebateRate; `feesEnabled` | 2026-09-19 |
| Рибейты мейкеру | polymarket/maker-rebates.md | доли по категориям, формула, выплата в pUSD, минимум 1 $ | 2026-09-19 |
| Награды за ликвидность | polymarket/liquidity-rewards.md | квадратичный скоринг, max spread, min size, эпохи | 2026-09-19 |
| Рибейты тейкеру | polymarket/taker-rebates.md | уровни по взвешенному объёму | 2026-09-19 |
| Стакан и цены | polymarket/prices-order-books.md | `minOrderSize` 5, `tickSize` 0.01, /book, /midpoint, /spread; prices-history и глубина хранения | 2026-09-19 |
| История сделок и цен | polymarket/data-api-openapi.json | `/v2/trades`: окно 3 года по condition, limit ≤ 1000, cursor; `/v2/prices-history`; 429 + Retry-After; без ключа | 2026-09-19 |
| WebSocket рынка | polymarket/realtime-data.md, polymarket/asyncapi.json | каналы, подписка, схемы сообщений | 2026-09-19 |
| Поиск рынков | polymarket/discover-markets.md, polymarket/market-details.md | Gamma API: события, рынки, поля condition_id, token ids, neg risk | 2026-09-19 |
| Ордера | polymarket/place-orders.md, manage-orders.md, order-lifecycle.md, clob-openapi.yaml | типы GTC/GTD/FOK/FAK, postOnly, `fee_rate_bps` в сделке, статусы | 2026-09-19 |
| Движок | polymarket/matching-engine.md | перезапуски, post-only режим, Retry-After | 2026-09-19 |
| Понятия | polymarket/concepts-*.md | рынок/событие, neg-risk, токены исходов и выплата 1 $, разрешение | 2026-09-19 |
| SDK | polymarket/python-sdk.md, polymarket/py-sdk-readme.md, polymarket/api-overview.md | `polymarket-client`, Public/Secure клиенты, базовые URL, L1/L2 | 2026-09-19 |

## Polymarket — как правильно

| Область | Источник | Отвечает на | Загружен |
|---|---|---|---|
| Маркет-мейкинг | polymarket/market-making.md | типы ордеров для котирования, риск-контроль, kill switch, батчи, реальное время вместо опроса | 2026-09-19 |
| Смысл площадки | polymarket/polymarket-101.md, concepts-prices-orderbook.md | цена как вероятность, чем является рынок предсказаний | 2026-09-19 |

## MCP — как устроено и как правильно

| Область | Источник | Отвечает на | Загружен |
|---|---|---|---|
| SDK | mcp/sdk-servers.md, sdk-tools.md, sdk-structured-output.md, sdk-handling-errors.md, sdk-resources.md, sdk-run.md, sdk-testing.md, sdk-client.md | MCPServer, @tool, схема из аннотаций, ToolError, ресурсы, stdio, клиент в процессе | 2026-09-19 |
| Спецификация | mcp/spec-tools.md, spec-resources.md (руководство), spec-stdio.md | имена и аннотации инструментов, isError, stdout только для кадров | 2026-09-19 |

## Kalshi — как устроено (для адаптера данных)

| Область | Источник | Отвечает на | Загружен |
|---|---|---|---|
| Рыночные данные | kalshi/quick-start-market-data.md, get-markets.md, get-market.md, get-market-orderbook.md, get-trades.md, get-events.md, rate-limits.md | публичные REST-эндпоинты без ключа, формат стакана (только bids yes/no), лимиты | 2026-09-19 |
| WebSocket | kalshi/websocket-connection.md, websocket-orderbook-updates.md | соединение требует аутентификации; канал orderbook_delta | 2026-09-19 |

## Инструменты

| Область | Источник | Отвечает на | Загружен |
|---|---|---|---|
| uv | python/uv-project-layout.md, python/uv-locking-syncing.md | pyproject, uv.lock, sync в CI | 2026-09-03 |
| pytest | python/pytest-fixtures.rst | фикстуры, conftest | 2026-09-03 |
| GitHub Actions | tooling/github-actions-workflow-syntax.md | синтаксис workflow | 2026-08-22 |
| Docker Compose | tooling/compose-spec.md | спецификация compose | 2026-08-22 |
| Лицензия | licenses/mit.txt | текст MIT | 2026-09-03 |

Руководства рода «как правильно» для uv, pytest и Docker в хранилище нет — записано, не оставлено пустым.

## Читается вживую, не кэшируется

| Область | Ссылка | Почему |
|---|---|---|
| Условия использования | https://polymarket.com/tos (Google Doc от 11.08.2026, читать mobilebasic-версию) | юридический документ; закрыт для США, читать из Узбекистана |
| Географические ограничения | https://help.polymarket.com/en/articles/13364163-geographic-restrictions и https://docs.polymarket.com/api-reference/geoblock (ссылка из ToS) | меняются без предупреждения |
| Действующие комиссии | https://help.polymarket.com/en/articles/13364478-trading-fees | ставки меняются, docs могут отставать |
| Право Узбекистана | https://lex.uz/docs/3806048 | ПП-3832 п. 3(д): операции с крипто-активами только через национальных провайдеров |
| Версия SDK | https://pypi.org/project/polymarket-client/ | версия и лицензия меняются |
| Условия Kalshi | https://kalshi.com/terms | юридический документ; автоматизированный доступ и использование данных |
