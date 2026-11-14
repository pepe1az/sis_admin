# ClickHouse 3 × 2 и ClickHouse Keeper на стенде группы 2

Этот документ описывает интеграцию кластера ClickHouse из проекта
`devops-itmo` в текущий стенд `sis_admin`. Кластер разворачивается отдельным
плейбуком на уже созданных ВМ. В рамках замены ZooKeeper на ClickHouse Keeper
из базовой части удалены три ZK-заглушки `g2-zk1..3`, их firewall и группа
`zookeeper` в шаблоне inventory. В `verify.yml` обновлён только адрес
cross-DC `traceroute` (вместо удалённой ZK-ВМ — узел ClickHouse); остальная
логика `site.yml`/`verify.yml` не менялась.

## Что разворачивается

| ВМ | ДЦ | Шард / реплика | Keeper |
|---|---|---|---|
| `g2-ch1r1` | dc1 | 1 / dc1 | да, id=1 |
| `g2-ch1r2` | dc2 | 1 / dc2 | — |
| `g2-ch2r1` | dc2 | 2 / dc2 | да, id=2 |
| `g2-ch2r2` | dc3 | 2 / dc3 | — |
| `g2-ch3r1` | dc3 | 3 / dc3 | да, id=3 |
| `g2-ch3r2` | dc1 | 3 / dc1 | — |

- 3 шарда × 2 реплики; реплики каждого шарда — в разных ДЦ.
- На каждой ВМ — один инстанс ClickHouse (`clickhouse-server@ch1`) на
  стандартных портах 9000/8123/9009/9004, метрики — 9363.
- Keeper — на трёх ВМ, по одной в каждом ДЦ; клиентский порт 9181, Raft 9234,
  метрики 9365. Разрешены только `mntr,ruok,srvr`.
- Схема `bench.events_local` (`ReplicatedMergeTree`) и `bench.events`
  (`Distributed`) создаётся идемпотентно через `ON CLUSTER`.
- Постоянная нагрузка — один генератор на `g2-ch1r1` (вставки + аналитика),
  чтобы не перегружать шесть ВМ с 1 ГиБ RAM.

Отдельных ZooKeeper-ВМ в стенде больше нет: `g2-zk1..3` были заглушками и
удалены. ClickHouse координируется через ClickHouse Keeper. По заданию
ZooKeeper был обязательным пунктом, а Keeper — продвинутым вариантом; команда
согласовала замену, поэтому в отчётах нужно явно указать это отклонение.
Если понадобится вернуть ZooKeeper, добавьте его узлы в `<zookeeper>` в
`roles/clickhouse/templates/config.xml.j2` и не запускайте роль Keeper.

## Отличия от исходного проекта `devops-itmo`

| | `devops-itmo` | здесь |
|---|---|---|
| ВМ | 3, по 2 инстанса ClickHouse | 6 уже созданных ВМ, по 1 инстансу |
| Keeper | на каждой из 3 ВМ | на 3 из 6 ВМ, по одному на ДЦ |
| Firewall | ufw | nftables, отдельная таблица `inet g2_clickhouse` |
| Коллекции | `community.general`, `ansible.posix` | только `ansible-core` |
| Нагрузка | на каждой ВМ | на одной выбранной ВМ |
| Секрет | инвентарь/vault | `-e @.local/clickhouse.yml` или vault |

`ufw` заменён на nftables по двум причинам: в `sis_admin` уже используется
nftables, а ufw блокирует ICMP, который проверяет существующий `verify.yml`
(направленные ping между гостями). Новая таблица не вызывает `flush ruleset`,
поэтому не трогает правила chaos-сценариев.

## Предварительные условия

1. Стенд создан: `site.yml` отработал, ВМ `g2-ch*` доступны, в
   `inventory/guests.generated.yml` есть группа `clickhouse`.
2. На ВМ Debian 13, есть исходящий HTTPS до `packages.clickhouse.com`.
3. На контроллере установлен `ansible-core` из `requirements.txt`.
   Дополнительные коллекции не нужны.

## Пароль пользователя default

Пароль не хранится в Git. Сгенерируйте локальный файл:

```bash
./scripts/clickhouse-secret.py
# created: .local/clickhouse.yml
```

`.local/` уже исключён из Git. Передавайте файл явно:

```bash
ansible-playbook clickhouse.yml \
  -i inventory/guests.generated.yml \
  -e @.local/clickhouse.yml
```

В production используйте `ansible-vault`:

```bash
ansible-playbook clickhouse.yml \
  -i inventory/guests.generated.yml \
  -e @inventories/prod/clickhouse-vault.yml --ask-vault-pass
```

Пароль должен быть одинаковым на всех шести узлах: иначе не будут работать
распределённые запросы и `remote()`.

## Развёртывание

```bash
# 1. Убедиться, что стенд и гости живы (существующая проверка группы).
.venv/bin/ansible-playbook verify.yml

# 2. Развернуть ClickHouse + Keeper.
.venv/bin/ansible-playbook clickhouse.yml \
  -i inventory/guests.generated.yml \
  -e @.local/clickhouse.yml

# 3. Проверить кластер.
.venv/bin/ansible-playbook clickhouse-verify.yml \
  -i inventory/guests.generated.yml \
  -e @.local/clickhouse.yml
```

Первый запуск скачивает пакеты ClickHouse (около 200 МБ на узел) и занимает
5–15 минут. Повторный запуск идемпотентен: конфиги не меняются, сервисы не
перезапускаются без необходимости.

## Проверки `clickhouse-verify.yml`

- Keeper: `ruok` → `imok`, `mntr`, `srvr`; ровно один лидер; `conf` не
  выполняется.
- ClickHouse: 6 узлов в `system.clusters`, макросы `{shard}`/`{replica}`,
  движки `ReplicatedMergeTree`/`Distributed`, `total_replicas=active_replicas=2`.
- Вставка 500 строк в `Distributed` и чтение обратно.
- Репликация: 500 × 2 = 1000 копий на шести локальных таблицах.
- Prometheus-метрики на всех шести инстансах и на трёх Keeper.
- Нагрузка действительно выполняет аналитические запросы (`system.query_log`).
  Проверка аналитики идёт с первого узла группы; генератор по умолчанию там же
  (`g2-ch1r1`). Если меняете `clickhouse_load_hosts`, поправьте `hosts` второго
  play в `clickhouse-verify.yml` или запускайте проверку с этого узла.

## Сеть и мониторинг

Роль `clickhouse_firewall` создаёт таблицу `inet g2_clickhouse`:

- порты 9000, 8123, 9009, 9004, 9363, 9181, 9234, 9365 доступны только
  IP шести узлов ClickHouse;
- порты 9181, 9363, 9365 дополнительно доступны IP из
  `clickhouse_monitoring_ips` (по умолчанию — `lab_monitoring_sources`);
- ICMP, SSH, established-соединения не блокируются;
- политика `input` — `accept`, ограничиваются только порты кластера.

Чтобы Prometheus группы 1 мог собирать метрики, добавьте его IP в inventory
или переопределите переменную:

```yaml
clickhouse_monitoring_ips:
  - 10.22.1.40
```

Цели Prometheus:

```
10.22.1.11:9363  # g2-ch1r1
10.22.2.11:9363  # g2-ch1r2
10.22.2.12:9363  # g2-ch2r1
10.22.3.12:9363  # g2-ch2r2
10.22.3.13:9363  # g2-ch3r1
10.22.1.13:9363  # g2-ch3r2
10.22.1.11:9365  # Keeper g2-ch1r1
10.22.2.12:9365  # Keeper g2-ch2r1
10.22.3.13:9365  # Keeper g2-ch3r1
```

Фактические IP берите из `inventory/guests.generated.yml`; Keeper-порты есть
только у `g2-ch1r1`, `g2-ch2r1`, `g2-ch3r1`.

## Ограничения

- Шесть ВМ по 1 ГиБ RAM — учебный профиль. Лимиты выставлены в
  `group_vars/clickhouse.yml` (`600M` на инстанс, `256M` на запрос).
  Для нагрузки/аналитики увеличьте RAM ВМ и лимиты.
- Keeper живёт на тех же ВМ, что и ClickHouse. Отказ ВМ `g2-ch1r1` уносит
  один из трёх Keeper и одну реплику шарда 1. Для production вынесите Keeper
  на отдельные ВМ (роль уже параметризована через `clickhouse_keeper_nodes`).
- Данные ClickHouse лежат в `/var/lib/clickhouse/instances` на корневом диске:
  роли RAID/LVM/XFS в этом репозитории нет, её делает ответственный за
  хранилище. После монтирования RAID переопределите `clickhouse_data_root` и
  перезапустите плейбук (данные нужно перенести до смены пути).
- Нет TLS, резервного копирования и Alertmanager; это отдельные задачи.
- `clickhouse-verify.yml` использует `remote()` с паролем в тексте запроса.
  Это приемлемо для проверки, но не для прикладного кода.
- Версия ClickHouse зафиксирована (`25.8.33.6`, apt hold). Обновление —
  осознанное действие: снять hold, изменить версию, прогнать плейбук и
  проверку по одному узлу.

## Диагностика

| Симптом | Что проверить |
|---|---|
| `apt` не видит ClickHouse | сеть/прокси до `packages.clickhouse.com`, ключ `/etc/apt/keyrings/clickhouse.asc` |
| Порт 9000 занят | `systemctl status clickhouse-server`; роль маскирует штатный сервис |
| Keeper не собирает кворум | `server_id`, порты 9181/9234, IP в `raft_configuration`, `/var/log/clickhouse-keeper/` |
| `ON CLUSTER` зависает | кворум Keeper, `system.distributed_ddl_queue` |
| Реплика не догоняет | `system.replicas.absolute_delay`, `system.replication_queue`, порт 9009 |
| Нет метрик в Prometheus | IP не в `clickhouse_monitoring_ips`, таблица `nft list table inet g2_clickhouse` |
| `verify.yml` группы 2 упал после установки | проверьте, что nftables не блокирует ICMP: `nft list table inet g2_clickhouse` |

Логи: `/var/log/clickhouse-server/ch1/`, `/var/log/clickhouse-keeper/`,
`journalctl -u 'clickhouse-server@*' -u clickhouse-keeper`.
