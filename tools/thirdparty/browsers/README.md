# Браузеры — почему здесь нет кода

Мост браузеров не хранит исходников в программе: сервер ставится и
обновляется сам, командой `npx -y @playwright/mcp@latest`, так написано
в README Playwright MCP. Копия пакета в репозитории устаревала бы молча,
а обновлять её руками — работа, которую npx делает без нас.

Папка нужна как рабочая при запуске сервера: лаунчер
`tools/dbapp/launchers/browsers_bridge_launcher.py` запускает npx отсюда.

| Что | Где |
| --- | --- |
| Сам сервер | пакет `@playwright/mcp` из npm, скачивает npx |
| Выбор браузера и профиля | рядом с настройками opencode: `mcp-browsers.json` |
| Профиль нейросети | `cache/browsers/profile-<браузер>` рядом с настройками |
| Лаунчер | `tools/dbapp/launchers/browsers_bridge_launcher.py` |
| Запись в реестре | `данные/mcp-registry.json`, сервер `browsers` |

Сборка Firefox для Playwright (`npx playwright install firefox`, около
90 МБ) ложится туда, куда её кладёт сам Playwright, и к программе
отношения не имеет.

Лицензия Playwright MCP — Apache-2.0, репозиторий:
https://github.com/microsoft/playwright-mcp
