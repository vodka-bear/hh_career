# hh-career

Бот для поиска подходящих вакансий на [hh.ru](https://hh.ru) и откликов через Telegram.

Периодически обходит заданные поисковые URL, оценивает вакансии через OpenAI-совместимый API (в том числе xAI/Grok) относительно резюме с hh.ru и присылает в Telegram только те, что проходят порог совпадения. Из чата можно откликнуться одной кнопкой.

## Возможности

- периодический опрос поисков hh.ru по URL из `filters.yaml`
- оценка соответствия вакансии резюме через LLM
- уведомления в Telegram с кнопкой «Откликнуться»
- хранение уже обработанных вакансий в SQLite
- работа с авторизованной сессией hh.ru через cookies

## Требования

- Python 3.11+
- Telegram-бот (`TELEGRAM_BOT_TOKEN`) и ваш `TELEGRAM_CHAT_ID`
- API-ключ OpenAI или xAI (`OPENAI_API_KEY` / `XAI_API_KEY`)
- cookies авторизованной сессии hh.ru

## Установка

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

Скопируйте примеры конфигов и заполните секреты:

```bash
cp .env.example .env
cp config.example.yaml config.yaml
cp filters.example.yaml filters.yaml
```

В `.env` укажите:

| Переменная | Описание |
|---|---|
| `OPENAI_API_KEY` или `XAI_API_KEY` | ключ LLM-провайдера |
| `TELEGRAM_BOT_TOKEN` | токен бота от BotFather |
| `TELEGRAM_CHAT_ID` | числовой id чата, куда слать вакансии |
| `HH_COOKIES` | опционально: cookies строкой вместо файла |

Cookies можно передать файлом (`--cookies`) или переменной `HH_COOKIES` (строка `name=value; ...` либо JSON из Cookie-Editor).

## Конфигурация

### `config.yaml`

| Параметр | Описание | По умолчанию |
|---|---|---|
| `poll_interval_minutes` | интервал между циклами поиска | `5` |
| `min_match_percent` | минимальный процент совпадения для уведомления | `70` |
| `request_delay_seconds` | пауза между HTTP-запросами к hh.ru | `2` |
| `http_timeout_seconds` | таймаут HTTP-запросов | `30` |
| `hh_base_url` | базовый URL hh.ru (можно региональный, например `https://krasnoyarsk.hh.ru`) | `https://hh.ru` |
| `database_path` | путь к SQLite | `data/hh.sqlite` |
| `filters_path` | путь к файлу поисков | `filters.yaml` |
| `openai.base_url` | OpenAI-совместимый endpoint | `https://api.openai.com/v1` |
| `openai.model` | модель | `gpt-4o-mini` |
| `openai.temperature` | температура генерации | `0.2` |

### `filters.yaml`

Список поисков. URL берётся из адресной строки поиска hh.ru. `pages` — сколько первых страниц обходить (нумерация на сайте с нуля: `pages: 2` → `page=0` и `page=1`).

```yaml
searches:
  - name: python-backend
    url: "https://hh.ru/search/vacancy?text=Python+backend&area=1&..."
    pages: 1
```

## Запуск

```bash
# через точку входа
hh-career --config config.yaml --cookies cookies.txt

# или как модуль
python -m hh_career --config config.yaml --cookies cookies.txt
```

### Параметры CLI

| Параметр | Описание | По умолчанию |
|---|---|---|
| `--config` | путь к `config.yaml` | `config.yaml` |
| `--cookies` | файл cookies hh.ru | не задан (тогда используется `HH_COOKIES`) |

После старта бот начинает polling Telegram и фоновый цикл поиска. В чате команда `/start` подтверждает, что бот слушает ваш chat id.

## Тесты

```bash
pytest
```

## Структура

```
src/hh_career/   — код приложения
tests/           — тесты
config.example.yaml
filters.example.yaml
.env.example
```

Локальные секреты и данные (`.env`, `config.yaml`, `filters.yaml`, cookies, SQLite) в репозиторий не коммитятся — см. `.gitignore`.
