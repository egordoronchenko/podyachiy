"""Поддельный каталог для тестов входа через домен — вместо соединения ldap3 (directory.connect).

Ведёт себя как контроллер Active Directory в той мере, в какой сервис к нему обращается:
простая привязка с подкодами отказа в сообщении («data 52e»), поиск по фильтру RFC 4515 с
правилами IN_CHAIN (вложенные группы) и BIT_AND, отказ на несуществующую базу. Фильтр
разбирается по-настоящему: значение, которое вырвалось бы из своего места в фильтре,
изменило бы результат поиска — так проверяется экранирование.

Всё, что сервис прислал, записывается: привязки (с паролями), фильтры, число соединений —
тесты смотрят, что именно ушло в каталог и чего туда не уходило.
"""
from app import directory

BASE = "DC=corp,DC=test"
BIND_DN = f"CN=svc-podyachiy,OU=Service,{BASE}"
BIND_PASSWORD = "служебный-пароль-Щ7"
URLS = ("ldaps://dc1.corp.test:636", "ldaps://dc2.corp.test:636")
PASSWORD = "доменный-пароль-Ж5"


def unescape(value: str) -> str:
    out, i = [], 0
    while i < len(value):
        if value[i] == "\\":
            out.append(chr(int(value[i + 1:i + 3], 16)))
            i += 3
        else:
            out.append(value[i])
            i += 1
    return "".join(out)


def parse(text: str, at: int = 0):
    """Фильтр → дерево: ("&", [...]), ("|", [...]), ("!", узел), ("=", атрибут, правило, значение)."""
    if text[at] != "(":
        raise ValueError(f"фильтр: ждал «(» на месте {at}: {text}")
    at += 1
    if text[at] in "&|":
        kind, kids, at = text[at], [], at + 1
        while text[at] == "(":
            node, at = parse(text, at)
            kids.append(node)
        if text[at] != ")":
            raise ValueError(f"фильтр: ждал «)» на месте {at}: {text}")
        return (kind, kids), at + 1
    if text[at] == "!":
        node, at = parse(text, at + 1)
        if text[at] != ")":
            raise ValueError(f"фильтр: ждал «)» на месте {at}: {text}")
        return ("!", node), at + 1
    end = text.index(")", at)
    item = text[at:end]
    if "(" in item or "=" not in item:
        raise ValueError(f"фильтр: негодное условие «{item}»")
    left, value = item.split("=", 1)
    attr, _, rule = left.rstrip(":").partition(":")
    return ("=", attr.lower(), rule, value), end + 1


class Forest:
    def __init__(self):
        self.entries: dict[str, dict[str, list[str]]] = {BASE: {"objectClass": ["top", "domain"]}}
        self.passwords: dict[str, str] = {}
        self.states: dict[str, str] = {}        # DN → подкод отказа: 533, 775, 532, 773, 701
        self.dead: set[str] = set()             # контроллеры, которые не отвечают
        self.binds: list[tuple[str, str, str]] = []     # (контроллер, DN, пароль) — все привязки
        self.filters: list[str] = []
        self.opened: list[str] = []             # к каким контроллерам сервис подключался
        self.entries[BIND_DN] = {"objectClass": ["top", "person", "user"], "sAMAccountName": ["svc-podyachiy"]}
        self.passwords[BIND_DN] = BIND_PASSWORD

    def dn(self, name: str) -> str:
        return next(dn for dn, attrs in self.entries.items()
                    if name.lower() in (v.lower() for v in attrs.get("sAMAccountName", [])))

    def user(self, login: str, name: str, groups=(), password: str = PASSWORD, mail: str | None = None,
             disabled: bool = False, upn: str | None = None, state: str = "") -> str:
        dn = f"CN={name},OU=People,{BASE}"
        self.entries[dn] = {"objectClass": ["top", "person", "organizationalPerson", "user"],
                            "objectCategory": [f"CN=Person,CN=Schema,CN=Configuration,{BASE}"],
                            "sAMAccountName": [login], "displayName": [name],
                            "userPrincipalName": [upn or f"{login}@corp.test"],
                            "userAccountControl": ["514" if disabled else "512"]}
        if mail != "":
            self.entries[dn]["mail"] = [mail or f"{login}@corp.test"]
        self.passwords[dn] = password
        if disabled:
            self.states[dn] = "533"
        if state:
            self.states[dn] = state
        for group in groups:
            self.entries[self.dn(group)].setdefault("member", []).append(dn)
        return dn

    def group(self, name: str, inside=()) -> str:
        dn = f"CN={name},OU=Groups,{BASE}"
        self.entries[dn] = {"objectClass": ["top", "group"], "sAMAccountName": [name], "cn": [name], "member": []}
        for outer in inside:
            self.entries[self.dn(outer)]["member"].append(dn)
        return dn

    def leave(self, login: str, group: str) -> None:
        self.entries[self.dn(group)]["member"].remove(self.dn(login))

    def within(self, dn: str, group_dn: str, seen=None) -> bool:
        """Входит ли запись в группу — напрямую или через вложенные группы."""
        seen = seen or set()
        for member in self.entries.get(group_dn, {}).get("member", []):
            if member.lower() == dn.lower():
                return True
            if member not in seen and "group" in self.entries[member]["objectClass"]:
                seen.add(member)
                if self.within(dn, member, seen):
                    return True
        return False

    def matches(self, dn: str, node) -> bool:
        kind = node[0]
        if kind == "&":
            return all(self.matches(dn, kid) for kid in node[1])
        if kind == "|":
            return any(self.matches(dn, kid) for kid in node[1])
        if kind == "!":
            return not self.matches(dn, node[1])
        _, attr, rule, value = node
        held = next((v for k, v in self.entries[dn].items() if k.lower() == attr), [])
        if rule == directory.IN_CHAIN:
            return attr == "memberof" and self.within(dn, unescape(value))
        if rule == directory.BIT_AND:
            return bool(held) and int(held[0]) & int(value) == int(value)
        if attr == "objectcategory" and value.lower() == "person":
            return any(v.lower().startswith("cn=person,") for v in held)
        if value == "*":
            return bool(held)
        return unescape(value).lower() in (v.lower() for v in held)

    def connect(self, url: str, cfg):
        if url in self.dead:
            raise directory.Unreachable("timed out")
        self.opened.append(url)
        return FakeLink(self, url)


class FakeLink:
    def __init__(self, forest: Forest, url: str):
        self.forest, self.url = forest, url

    def bind(self, dn: str, password: str) -> None:
        forest = self.forest
        forest.binds.append((self.url, dn, password))
        if not password:
            return          # анонимная привязка: настоящий контроллер отвечает на неё «успех»
        code = "52e"
        if dn in forest.passwords:
            state = forest.states.get(dn, "")
            if state == "775":                              # блокировку контроллер называет при любом пароле
                code = "775"
            elif forest.passwords[dn] == password:
                code = state
        if code:
            raise directory.Refused("invalidCredentials",
                                    f"80090308: LdapErr: DSID-0C090447, comment: AcceptSecurityContext error, "
                                    f"data {code}, v3839")

    def search(self, base, flt, attrs=(), scope="sub", limit=0):
        forest = self.forest
        forest.filters.append(flt)
        if base not in forest.entries:
            raise directory.Refused("noSuchObject", f"0000208D: NameErr: DSID-0310028B, problem 2001 (NO_OBJECT) {base}")
        node, end = parse(flt)
        if end != len(flt):
            raise directory.Refused("protocolError", f"лишнее после фильтра: {flt[end:]}")
        found = []
        for dn, held in forest.entries.items():
            inside = dn == base if scope == "base" else dn.lower().endswith(base.lower())
            if inside and forest.matches(dn, node):
                found.append((dn, {name: list(held[name]) for name in attrs if name in held}))
                if limit and len(found) >= limit:
                    break
        return found

    def peer(self) -> dict:
        return {"tls": "TLSv1.3", "subject": "CN=dc1.corp.test", "issuer": "CN=Corp Root CA",
                "until": "01.01.2031"}

    def close(self) -> None:
        pass


def staffed() -> Forest:
    """Домен для тестов: группа доступа, группа администраторов, вложенная группа и люди."""
    forest = Forest()
    forest.group("podyachiy-users")
    forest.group("podyachiy-admins", inside=["podyachiy-users"])
    forest.group("otdel-prodazh", inside=["podyachiy-users"])
    forest.user("ivanov", "Иванов Иван Иванович", ["podyachiy-users"])
    forest.user("Petrova", "Петрова Анна Сергеевна", ["podyachiy-admins"])
    forest.user("sidorov", "Сидоров Пётр Алексеевич", ["otdel-prodazh"])            # через вложенную группу
    forest.user("kuznetsov", "Кузнецов Олег Николаевич")                             # ни в одной группе
    forest.user("smirnova", "Смирнова Елена Викторовна", ["podyachiy-users"], disabled=True)
    forest.user("orlov", "Орлов Дмитрий Павлович", ["podyachiy-users"], state="773")
    forest.user("volkov", "Волков Сергей Андреевич", ["podyachiy-users"], state="532")
    forest.user("zaitsev", "Зайцев Михаил Юрьевич", ["podyachiy-users"], state="775")
    forest.user("belov", "Белов Артём Игоревич", ["podyachiy-users"], state="701")
    return forest
