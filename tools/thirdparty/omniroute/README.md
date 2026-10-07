# OmniRoute — что здесь лежит и почему

Это папка моста OmniRoute. В репозитории от неё только два файла:
`package.json` и `package-lock.json`. Сам пакет сюда ставит программа
кнопкой «Развернуть»:

```
npm install omniroute@latest
```

Так советует README автора (https://github.com/diegosouzapw/OmniRoute):
пакет ставится из npm. Глобальную установку (`npm install -g`) программа
не делает: она писала бы в служебные папки Node.js, а обновлять её
пришлось бы руками. Здесь папка своя, и удаляется она вместе с базой.

`node_modules/` в репозиторий не попадает — он закрыт правилом в
`.gitignore`, как у моста OBS (`tools/thirdparty/obs-mcp-node`). На новой
машине папка создаётся заново кнопкой «Развернуть».

| Что | Где |
| --- | --- |
| Сам шлюз | пакет `omniroute` из npm, ставит программа в `node_modules/` |
| Точка входа | `node_modules/omniroute/bin/omniroute.mjs` |
| Лаунчер MCP | `tools/dbapp/launchers/omniroute_bridge_launcher.py` |
| Панель и API | `http://127.0.0.1:20128` (порт из README) |
| Данные шлюза | папка `omniroute-data` рядом с настройками opencode |
| Запись в реестре | `данные/mcp-registry.json`, сервер `omniroute` |

Что внутри папки данных: база SQLite, журналы и созданный самим OmniRoute
файл `.env` с ключом шифрования хранилища. Там же оказываются ключи
провайдеров, которые человек вписывает в панели. Это секреты этой машины,
поэтому папка закрыта правилом в `.gitignore` и в `opencode.jsonc` не
попадает.

Проверено 07.10.2026 на версии 3.8.51: `GET /api/health` отвечает
`{"status":"ok"}`, MCP-сервер по stdio отдаёт 110 инструментов, протокол
2025-06-18.

Лицензия OmniRoute — MIT, репозиторий:
https://github.com/diegosouzapw/OmniRoute
