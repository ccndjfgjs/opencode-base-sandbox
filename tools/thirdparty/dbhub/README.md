# DBHub — почему здесь нет кода

Мост DBHub не хранит исходников в программе: сервер ставится и
обновляется сам, командой `npx -y @bytebase/dbhub@latest`, так написано
в README Bytebase. Копия пакета в репозитории устаревала бы молча, а
обновлять её руками — работа, которую npx делает без нас.

Папка нужна как рабочая при запуске сервера: лаунчер
`tools/dbapp/launchers/dbhub_bridge_launcher.py` запускает npx отсюда.

| Что | Где |
| --- | --- |
| Сам сервер | пакет `@bytebase/dbhub` из npm, скачивает npx |
| Подключения и пароли | рядом с настройками opencode: `mcp-dbhub.toml` и `mcp-dbhub-<имя>-password.txt` |
| Лаунчер | `tools/dbapp/launchers/dbhub_bridge_launcher.py` |
| Запись в реестре | `данные/mcp-registry.json`, сервер `dbhub` |

Лицензия DBHub — MIT, репозиторий: https://github.com/bytebase/dbhub
