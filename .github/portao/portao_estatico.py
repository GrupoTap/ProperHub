#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
portao_estatico.py — portão de PR, parte ESTÁTICA (sem banco, sem segredo, segundos). ProperCare-backlog#41.

FONTE: `ferramentas/portao/portao_estatico.py` do backlog. Cada repositório de código tem uma CÓPIA IGUAL em
`.github/portao/portao_estatico.py` (a parte com banco, no backlog, reprova cópia diferente). Mudou aqui ⇒ PR curta
em cada repositório com a cópia nova.

  python3 portao_estatico.py [--raiz DIR] [--base REF | --tudo | ARQUIVO ...] [--json SAIDA] [--autoteste]

  --base REF   confere os .html/.js que a PR muda (git diff REF...HEAD na raiz) — é o que o workflow da PR roda
  --tudo       confere todo .html/.js do repositório (varredura da main)
  ARQUIVO ...  só esses (caminho relativo à raiz)
  --json F     grava os achados em F (a parte com banco lê)
  --autoteste  a bancada da ferramenta: pares que passam e que TÊM de reprovar
  --chamar-backlog  (2º passo do workflow) dispara o workflow `portao` do ProperCare-backlog para esta PR e espera:
               env PORTAO_TOKEN (Secret), REPO, PR, SHA. Só traz ✅/❌ e o NOME do passo que reprovou (nada privado)

O que confere (cada <script> lido por um leitor de HTML de verdade — `html.parser` da biblioteca padrão: <script>
citado dentro de comentário <!-- --> ou dentro de uma string de outro script NÃO vira bloco):
  1. SINTAXE: `node --check` em cada bloco clássico (e em cada .js do repositório); módulo (type=module) como .mjs.
  2. let/const/class REPETIDO ENTRE BLOCOS (ou let/const/class × var/function de outro bloco): o navegador recusa o
     bloco de baixo INTEIRO (SyntaxError na entrada) — nada daquele bloco roda. Lido pelo acorn (só a declaração de topo).
  3. FUNÇÃO GLOBAL DECLARADA 2× (entre blocos ou no mesmo bloco): a de baixo vence CALADA. Só passa com a marca
     `// ⚠ SOBRESCREVE <nome>` na linha da declaração de baixo ou nas 3 linhas acima dela.
  <script src="x.js"> local entra na ordem (mesmo escopo global); src externo (http) e tipos que não são JS ficam fora.
  Bibliotecas de terceiros (*.min.js, jspdf*, html2canvas*, vendor/) ficam fora: não são código nosso.

Limites conhecidos (auditoria de 01/11): `"<!--<script>"` dentro de string JS (o navegador estende o bloco; aqui não);
`<script src=… />` tratado como fechado. Nenhum dos dois aparece nas mains.

Saída: 0 = ✅ · 1 = ❌ (lista com arquivo:linha) · 9 = uso/ambiente (node ou acorn ausente).
Só biblioteca padrão + node; o acorn (8.15.0) é instalado sozinho pelo npm em ~/.cache/portao (ou $PORTAO_CACHE)
na 1ª vez (PORTAO_ACORN=<pasta do pacote> usa outro).
"""
import sys, os, re, json, subprocess, tempfile, shutil, hashlib
from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor

VERSAO = "portao_estatico v3 (01/11/2026, ProperCare-backlog#41, depois das 2 auditorias)"
ACORN_VER = "8.15.0"
LIB_TERCEIROS = re.compile(r"(^|/)(vendor|node_modules)/|\.min\.js$|(^|/)(jspdf|html2canvas)[^/]*\.js$", re.I)
# os tipos que o navegador EXECUTA (lista do HTML Living Standard, "JavaScript MIME type essence match") + module
TIPOS_JS = {"", "module", "text/javascript", "application/javascript", "application/ecmascript", "application/x-ecmascript",
            "application/x-javascript", "text/ecmascript", "text/javascript1.0", "text/javascript1.1", "text/javascript1.2",
            "text/javascript1.3", "text/javascript1.4", "text/javascript1.5", "text/jscript", "text/livescript",
            "text/x-ecmascript", "text/x-javascript"}
INERTES = {"template", "noscript", "textarea", "title", "xmp", "iframe", "noembed", "noframes"}   # <script> aí dentro não roda
MARCA = re.compile(r"⚠\s*SOBRESCREVE\s+([A-Za-z_$][\w$]*)")


# ── leitor de HTML ─────────────────────────────────────────────────────────────────────────────────────
class _Scripts(HTMLParser):
    """Blocos <script> na ordem: dict(linha=<linha do 1º caractere do código>, tipo, src, codigo)."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.blocos, self._atual, self._inerte = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag in INERTES:
            self._inerte.append(tag)
            return
        if tag != "script":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        tipo = a.get("type", "").strip().lower().split(";")[0].strip()
        self._atual = dict(linha=self.getpos()[0], tipo=tipo, src=a.get("src", "").strip(), codigo="", _ini=None,
                           inerte=bool(self._inerte), aberto=False)

    def handle_data(self, data):
        if self._atual is not None:
            if self._atual["_ini"] is None:
                self._atual["_ini"] = self.getpos()[0]
            self._atual["codigo"] += data

    def handle_endtag(self, tag):
        if tag in INERTES:
            if tag in self._inerte:
                while self._inerte and self._inerte.pop() != tag:
                    pass
            return
        if tag == "script" and self._atual is not None:
            b = self._atual
            b["linha"] = b.pop("_ini") or b["linha"]
            self.blocos.append(b)
            self._atual = None

    def fechar(self):
        self.close()
        if self._atual is not None:          # <script> sem </script> (arquivo truncado): o navegador engole o resto
            b = self._atual
            b["linha"] = b.pop("_ini") or b["linha"]
            b["aberto"] = True
            self.blocos.append(b)
            self._atual = None


def blocos_do_html(caminho, raiz):
    """[(rotulo, linha_base, codigo, modo)] na ordem de execução. modo: 'script' | 'module'. Avisos à parte."""
    txt = open(caminho, encoding="utf-8", errors="replace").read()
    P = _Scripts()
    P.feed(txt)
    P.fechar()
    out, avisos, ruins = [], [], []
    for n, b in enumerate(P.blocos, 1):
        tipo = b["tipo"]
        if b["aberto"]:
            ruins.append((b["linha"], "bloco %d: <script> sem </script> — o navegador engole o resto do arquivo" % n))
        if b["inerte"] or tipo not in TIPOS_JS:
            continue                      # application/json, text/template, ld+json…: não é código
        modo = "module" if tipo == "module" else "script"
        if b["src"]:
            s = b["src"].split("?")[0].split("#")[0]
            if re.match(r"^(https?:)?//", s) or s.startswith("data:"):
                continue
            # "/x.js" = raiz do repositório (o site do GitHub Pages serve cada repo numa subpasta, mas o arquivo é este)
            alvo = os.path.normpath(os.path.join(raiz if s.startswith("/") else os.path.dirname(caminho), s.lstrip("/")))
            rel = os.path.relpath(alvo, raiz).replace("\\", "/")
            if rel.startswith("../") or os.path.islink(alvo):
                ruins.append((b["linha"], "<script src=\"%s\"> (bloco %d) aponta para FORA do repositório ou para um link: recusado" % (b["src"], n)))
                continue
            if LIB_TERCEIROS.search(rel):
                continue
            if not os.path.isfile(alvo):
                ruins.append((b["linha"], "<script src=\"%s\"> (bloco %d) não existe no repositório: a tela carrega um arquivo que não há" % (b["src"], n)))
                continue
            out.append(("%s (src do bloco %d)" % (rel, n), 1, open(alvo, encoding="utf-8", errors="replace").read(), modo, rel))
            continue
        out.append(("bloco %d" % n, b["linha"], b["codigo"], modo, None))
    return out, avisos, ruins


def srcs_locais(caminho, raiz, com_tipo=False):
    """os .js locais que um .html carrega por <script src> (relativos à raiz) — para a busca reversa.
       com_tipo=True → {rel: {'script'|'module'}} (o modo de leitura do .js vem de quem o carrega)."""
    try:
        P = _Scripts(); P.feed(open(caminho, encoding="utf-8", errors="replace").read()); P.fechar()
    except Exception:
        return {} if com_tipo else set()
    out = {}
    for b in P.blocos:
        s = b["src"].split("?")[0].split("#")[0]
        if s and not re.match(r"^(https?:)?//", s) and not s.startswith("data:"):
            alvo = os.path.normpath(os.path.join(raiz if s.startswith("/") else os.path.dirname(caminho), s.lstrip("/")))
            out.setdefault(os.path.relpath(alvo, raiz).replace("\\", "/"), set()).add("module" if b["tipo"] == "module" else "script")
    return out if com_tipo else set(out)


def modo_do_js(rel, raiz):
    """script, a não ser que TODO .html que carrega o .js o carregue como type=module (sem ninguém: script — sw.js)."""
    modos = set()
    for h in todos(raiz):
        if h.lower().endswith((".html", ".htm")):
            modos |= srcs_locais(os.path.join(raiz, h), raiz, com_tipo=True).get(rel, set())
    return "module" if modos == {"module"} else "script"


# ── node: sintaxe (node --check) e declarações de topo (acorn) ──────────────────────────────────────────
NODE_DECL = r"""
const acorn = require(process.argv[2]);
const blocos = JSON.parse(require('fs').readFileSync(process.argv[3], 'utf8'));
const out = [];
function nomes(p, acc) {
  if (!p) return acc;
  switch (p.type) {
    case 'Identifier': acc.push(p); break;
    case 'ObjectPattern': p.properties.forEach(q => nomes(q.type === 'RestElement' ? q.argument : q.value, acc)); break;
    case 'ArrayPattern': p.elements.forEach(q => nomes(q, acc)); break;
    case 'RestElement': nomes(p.argument, acc); break;
    case 'AssignmentPattern': nomes(p.left, acc); break;
  }
  return acc;
}
// var dentro de bloco/for/if/try/switch/label/while (sem entrar em função) também vira var GLOBAL
function vars(s, d) {
  if (!s) return;
  switch (s.type) {
    case 'VariableDeclaration':
      if (s.kind === 'var') for (const v of s.declarations) for (const id of nomes(v.id, [])) d.push({ tipo: 'var', nome: id.name, linha: id.loc.start.line });
      break;
    case 'BlockStatement': s.body.forEach(x => vars(x, d)); break;
    case 'IfStatement': vars(s.consequent, d); vars(s.alternate, d); break;
    case 'ForStatement': vars(s.init, d); vars(s.body, d); break;
    case 'ForInStatement': case 'ForOfStatement': vars(s.left, d); vars(s.body, d); break;
    case 'WhileStatement': case 'DoWhileStatement': case 'LabeledStatement': case 'WithStatement': vars(s.body, d); break;
    case 'TryStatement': vars(s.block, d); if (s.handler) vars(s.handler.body, d); vars(s.finalizer, d); break;
    case 'SwitchStatement': s.cases.forEach(c => c.consequent.forEach(x => vars(x, d))); break;
  }
}
for (const b of blocos) {
  let ast;
  try { ast = acorn.parse(b.codigo, { ecmaVersion: 'latest', sourceType: 'script', locations: true, allowHashBang: false }); }
  catch (e) { out.push({ erro: String(e.message), linha: (e.loc && e.loc.line) || 1 }); continue; }
  const d = [];
  for (const s of ast.body) {
    if (s.type === 'FunctionDeclaration' && s.id) d.push({ tipo: 'function', nome: s.id.name, linha: s.loc.start.line });
    else if (s.type === 'ClassDeclaration' && s.id) d.push({ tipo: 'class', nome: s.id.name, linha: s.loc.start.line });
    else if (s.type === 'VariableDeclaration')
      for (const v of s.declarations) for (const id of nomes(v.id, [])) d.push({ tipo: s.kind, nome: id.name, linha: id.loc.start.line });
    else vars(s, d);
  }
  out.push({ decl: d });
}
process.stdout.write(JSON.stringify(out));
"""


def _cache():
    return os.environ.get("PORTAO_CACHE") or os.path.join(os.path.expanduser("~"), ".cache", "portao")


def achar_acorn():
    p = os.environ.get("PORTAO_ACORN")
    if p and os.path.isfile(os.path.join(p, "package.json")):
        return p
    pasta = os.path.join(_cache(), "acorn-" + ACORN_VER)
    alvo = os.path.join(pasta, "node_modules", "acorn")
    if os.path.isfile(os.path.join(alvo, "package.json")):
        return alvo
    if not shutil.which("npm"):
        return None
    os.makedirs(pasta, exist_ok=True)
    r = subprocess.run(["npm", "install", "--silent", "--no-save", "--no-audit", "--no-fund", "--prefix", pasta,
                        "acorn@" + ACORN_VER], capture_output=True, text=True, timeout=180)
    return alvo if r.returncode == 0 and os.path.isfile(os.path.join(alvo, "package.json")) else None


def node_check(codigo, modo, tmp, n):
    f = os.path.join(tmp, "b%04d.%s" % (n, "mjs" if modo == "module" else "js"))
    with open(f, "w", encoding="utf-8") as fh:
        fh.write(codigo)
    r = subprocess.run(["node", "--check", f], capture_output=True, text=True, timeout=120)
    if r.returncode == 0:
        return None
    err = (r.stderr or r.stdout or "").strip()
    m = re.search(re.escape(f) + r":(\d+)", err)
    ln = int(m.group(1)) if m else 1
    msg = next((l.strip() for l in err.splitlines() if re.match(r"\s*(SyntaxError|ReferenceError|TypeError)", l)), err.splitlines()[-1] if err else "erro")
    return ln, msg


def declaracoes(blocos, acorn, tmp):
    f = os.path.join(tmp, "blocos.json")
    with open(f, "w", encoding="utf-8") as fh:
        json.dump([{"codigo": b} for b in blocos], fh)
    h = os.path.join(tmp, "decl.js")
    with open(h, "w", encoding="utf-8") as fh:
        fh.write(NODE_DECL)
    r = subprocess.run(["node", h, acorn, f], capture_output=True, text=True, timeout=300)
    if r.returncode:
        raise RuntimeError("leitor de declarações (acorn) falhou: " + r.stderr.strip()[-400:])
    return json.loads(r.stdout)


# ── conferência de um arquivo ──────────────────────────────────────────────────────────────────────────
def tem_marca(linhas, ln, nome):
    for i in range(max(0, ln - 4), min(len(linhas), ln)):
        for m in MARCA.finditer(linhas[i]):
            if m.group(1) == nome:
                return True
    return False


def conferir_arquivo(rel, raiz, acorn, tmp):
    """→ (achados ❌, avisos ⚠, resumo). Achado = dict(regra, arquivo, linha, msg)."""
    cam = os.path.join(raiz, rel)
    ach, av = [], []
    if rel.lower().endswith(".js"):
        blocos = [("arquivo", 1, open(cam, encoding="utf-8", errors="replace").read(), modo_do_js(rel, raiz), None)]
        sintaxe_so = True
    else:
        blocos, av, ruins = blocos_do_html(cam, raiz)
        sintaxe_so = False
        for ln, msg in ruins:
            ach.append(dict(regra="sintaxe", arquivo=rel, linha=ln, msg=msg))
    # 1 · sintaxe (node --check, em paralelo)
    base = os.path.join(tmp, re.sub(r"\W", "_", rel))
    os.makedirs(base, exist_ok=True)
    with ThreadPoolExecutor(max_workers=8) as ex:
        res = list(ex.map(lambda ib: node_check(ib[1][2], ib[1][3], base, ib[0]), enumerate(blocos)))
    quebrados = set()
    for i, (b, r) in enumerate(zip(blocos, res)):
        if r:
            quebrados.add(i)
            ln = (b[1] + r[0] - 1) if b[4] is None else r[0]
            ach.append(dict(regra="sintaxe", arquivo=b[4] or rel, linha=ln,
                            msg="%s não compila (node --check): %s" % (b[0], r[1])))
    resumo = dict(arquivo=rel, blocos=len(blocos), quebrados=len(quebrados))
    # 2 e 3 · declarações de topo entre os blocos clássicos (módulo tem escopo próprio). O acorn lê como SCRIPT
    # clássico, que é como o navegador lê: o node --check aceita `return`/`export`/`await` no topo (CommonJS e detecção
    # de módulo do Node 22), o navegador NÃO — então erro do acorn num bloco clássico é ❌ de sintaxe, não aviso.
    cls = [(i, b) for i, b in enumerate(blocos) if b[3] == "script" and i not in quebrados]
    dec = declaracoes([b[2] for _, b in cls], acorn, tmp) if cls else []
    lex, varf, funcs = {}, {}, {}
    nfun = 0
    for (i, b), d in zip(cls, dec):
        onde = b[4] or rel
        def ln_de(x):
            return (b[1] + x - 1) if b[4] is None else x
        if "erro" in d:
            ach.append(dict(regra="sintaxe", arquivo=onde, linha=ln_de(d.get("linha", 1)),
                            msg="%s não roda como script do navegador: %s" % (b[0], d["erro"][:200])))
            continue
        if sintaxe_so:
            continue
        linhas = b[2].split("\n")
        visto_aqui_lex = {}
        for x in d["decl"]:
            nome, tp, L = x["nome"], x["tipo"], ln_de(x["linha"])
            if tp in ("let", "const", "class"):
                ant = lex.get(nome) or varf.get(nome)
                if ant:
                    ach.append(dict(regra="colisao", arquivo=onde, linha=L,
                                    msg="`%s %s` (%s) já foi declarado no %s (%s:%d, `%s`): o navegador recusa o %s INTEIRO"
                                        % (tp, nome, b[0], ant[0], ant[1], ant[2], ant[3], b[0])))
                visto_aqui_lex[nome] = (b[0], onde, L, tp)
            else:  # var / function
                if nome in lex:
                    ant = lex[nome]
                    ach.append(dict(regra="colisao", arquivo=onde, linha=L,
                                    msg="`%s %s` (%s) colide com `%s %s` do %s (%s:%d): o navegador recusa o %s INTEIRO"
                                        % (tp, nome, b[0], ant[3], nome, ant[0], ant[1], ant[2], b[0])))
                if tp == "function":
                    nfun += 1
                    if nome in funcs:
                        ant = funcs[nome]
                        if not tem_marca(linhas, x["linha"], nome):
                            ach.append(dict(regra="funcao2x", arquivo=onde, linha=L,
                                            msg="function %s declarada 2× (a 1ª no %s, %s:%d): a de baixo vence calada. "
                                                "Se é de propósito, ponha `// ⚠ SOBRESCREVE %s` na linha acima"
                                                % (nome, ant[0], ant[1], ant[2], nome)))
                    funcs[nome] = (b[0], onde, L)
                varf.setdefault(nome, (b[0], onde, L, tp))
        for k, v in visto_aqui_lex.items():
            lex.setdefault(k, v)
    resumo.update(funcoes=nfun, lexicas=len(lex))
    return ach, av, resumo


def nosso(p):
    return p.lower().endswith((".html", ".htm", ".js")) and not LIB_TERCEIROS.search(p) and not p.startswith(".github/")


def todos(raiz):
    out = []
    for d, ds, fs in os.walk(raiz):
        ds[:] = [x for x in ds if not x.startswith(".") and x not in ("node_modules",)]
        for f in fs:
            rel = os.path.relpath(os.path.join(d, f), raiz).replace("\\", "/")
            if nosso(rel):
                out.append(rel)
    return sorted(out)


def ampliar(raiz, rels, apagados=()):
    """.js mudado ou apagado ⇒ entra todo .html que o carrega por <script src> (o escopo global é o da página)."""
    js = {r for r in list(rels) + list(apagados) if r.lower().endswith(".js")}
    out = set(r for r in rels if os.path.isfile(os.path.join(raiz, r)))
    if js:
        for h in todos(raiz):
            if h.lower().endswith((".html", ".htm")) and srcs_locais(os.path.join(raiz, h), raiz) & js:
                out.add(h)
    return sorted(out)


def mudados_git(raiz, base):
    """(mudados, apagados) entre base e HEAD — nome com acento sai certo (core.quotePath=false, -z)."""
    def lista(filtro):
        r = subprocess.run(["git", "-c", "core.quotePath=false", "-C", raiz, "diff", "-z", "--no-renames" if filtro == "D" else "--find-renames",
                            "--name-only", "--diff-filter=" + filtro, base + "...HEAD"], capture_output=True)
        if r.returncode:
            raise SystemExit("🔴 git diff %s...HEAD falhou: %s" % (base, r.stderr.decode(errors="replace").strip()))
        return [x for x in r.stdout.decode("utf-8", errors="replace").split("\0") if x]
    return [a for a in lista("ACMR") if nosso(a)], [a for a in lista("D") if nosso(a)]


def listar(raiz, modo, base=None, arquivos=None):
    if arquivos:
        return [a for a in arquivos if nosso(a) and os.path.isfile(os.path.join(raiz, a))]
    if modo == "base":
        m, d = mudados_git(raiz, base)
        return ampliar(raiz, [a for a in m if nosso(a)], d)
    out = []
    for d, ds, fs in os.walk(raiz):
        ds[:] = [x for x in ds if not x.startswith(".") and x not in ("node_modules",)]
        for f in fs:
            rel = os.path.relpath(os.path.join(d, f), raiz).replace("\\", "/")
            if nosso(rel):
                out.append(rel)
    return sorted(out)


def conferir(raiz, rels, acorn=None, falar=True):
    if not shutil.which("node"):
        raise SystemExit(9)
    acorn = acorn or achar_acorn()
    if not acorn:
        print("🔴 não achei nem instalei o acorn %s (npm) — sem ele a regra de colisão não roda" % ACORN_VER)
        raise SystemExit(9)
    tmp = tempfile.mkdtemp(prefix="portao_")
    try:
        ach, av, res = [], [], []
        for rel in rels:
            a, w, r = conferir_arquivo(rel, raiz, acorn, tmp)
            ach += a; av += w; res.append(r)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if falar:
        print(VERSAO)
        for r in res:
            print("  · %s — %d bloco(s)%s" % (r["arquivo"], r["blocos"], (", %d funções de topo" % r["funcoes"]) if "funcoes" in r else ""))
        if not rels:
            print("  · nenhum .html/.js nosso mudou — nada a conferir")
        for w in av:
            print("  ⚠ " + w)
        for x in ach:
            print("  ❌ [%s] %s:%d — %s" % (x["regra"], x["arquivo"], x["linha"], x["msg"]))
            if os.environ.get("GITHUB_ACTIONS"):
                print("::error file=%s,line=%d::%s" % (x["arquivo"], x["linha"], x["msg"].replace("\n", " ")))
        print("RESULTADO estáticas: %s" % ("✅ verde (%d arquivo(s))" % len(rels) if not ach else "❌ %d problema(s)" % len(ach)))
    return ach, av, res


# ── chamar a parte com banco (no backlog privado) e esperar ────────────────────────────────────────────
BACKLOG = "GrupoTap/ProperCare-backlog"
WF = "portao.yml"


def _api(metodo, caminho, tok, corpo=None):
    import urllib.request, urllib.error
    base = os.environ.get("PORTAO_API", "https://api.github.com").rstrip("/")
    req = urllib.request.Request(base + caminho, method=metodo, data=json.dumps(corpo).encode() if corpo is not None else None,
                                 headers={"Accept": "application/vnd.github+json", "Authorization": "Bearer " + tok,
                                          "User-Agent": "portao-propercare", "X-GitHub-Api-Version": "2022-11-28",
                                          **({"Content-Type": "application/json"} if corpo is not None else {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            t = r.read().decode() or "{}"
            return r.status, json.loads(t)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}


def chamar_backlog(espera=None, teto=None, janela=180):
    """Dispara o workflow `portao` do backlog (ref main) para esta PR, acha a execução pelo nome, espera e devolve 0/1."""
    import time
    espera = espera if espera is not None else int(os.environ.get("PORTAO_ESPERA", "15"))
    teto = teto if teto is not None else int(os.environ.get("PORTAO_TETO", str(45 * 60)))
    tok = os.environ.get("PORTAO_TOKEN", "").strip()
    repo, pr, sha = os.environ.get("REPO", ""), os.environ.get("PR", ""), os.environ.get("SHA", "")
    cab = os.environ.get("HEAD_REPO", repo)
    if cab.lower() != repo.lower():
        print("❌ PR vinda de outro repositório (%s): o portão não roda código de fora da GrupoTap. "
              "Abra a PR a partir de um branch DESTE repositório." % (cab or "apagado"))
        return 1
    if not tok:
        print("❌ falta o Secret PORTAO_TOKEN neste repositório (Settings › Secrets and variables › Actions). "
              "Roteiro: entregas/41/ROTEIRO_PORTAO.md do ProperCare-backlog.")
        return 1
    chamada = "%s-%s-%s" % (sha[:12], os.environ.get("GITHUB_RUN_ID", "0"), os.environ.get("GITHUB_RUN_ATTEMPT", "1"))
    st, j = _api("POST", "/repos/%s/actions/workflows/%s/dispatches" % (BACKLOG, WF), tok,
                 dict(ref="main", inputs=dict(repo=repo, pr=str(pr), sha=sha, chamada=chamada)))
    if st not in (200, 204):
        dica = {401: "o PORTAO_TOKEN venceu ou foi apagado — gere outro (roteiro, passo 1) e troque o Secret",
                403: "o PORTAO_TOKEN não tem a permissão Actions = Read and write no ProperCare-backlog",
                404: "o token não enxerga o ProperCare-backlog (repositório não escolhido no token) ou o workflow portao.yml ainda não está na main de lá",
                422: "o backlog recusou os dados da chamada"}.get(st, "resposta inesperada")
        print("❌ não consegui chamar o portão do backlog (HTTP %d): %s. %s" % (st, dica, str(j.get("message", ""))[:200]))
        return 1
    import datetime
    desde = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    print("chamei o portão do backlog (%s) — procurando a execução…" % chamada)
    t0, run = time.time(), None
    while time.time() - t0 < janela and run is None:
        st, j = _api("GET", "/repos/%s/actions/workflows/%s/runs?event=workflow_dispatch&per_page=100&created=%%3E%%3D%s" % (BACKLOG, WF, desde), tok)
        for r in (j.get("workflow_runs") or []) if st == 200 else []:
            if (r.get("display_title") or "").endswith(" " + chamada):
                run = r
                break
        if run is None:
            time.sleep(espera or 0.05)
    if run is None:
        print("❌ o backlog não começou a execução em 3 min (fila do GitHub cheia ou workflow quebrado) — use Re-run jobs")
        return 1
    url = run.get("html_url", "")
    print("execução no backlog: %s" % url)
    while run.get("status") != "completed" and time.time() - t0 < teto:
        time.sleep(espera)
        st, j = _api("GET", "/repos/%s/actions/runs/%s" % (BACKLOG, run["id"]), tok)
        if st == 200:
            run = j
    if run.get("status") != "completed":
        print("❌ o portão do backlog não terminou em %d min" % (teto // 60))
        return 1
    st, j = _api("GET", "/repos/%s/actions/runs/%s/jobs" % (BACKLOG, run["id"]), tok)
    passos = [s for jb in (j.get("jobs") or []) for s in (jb.get("steps") or [])]
    ruins = [s["name"] for s in passos if s.get("conclusion") == "failure"]
    avisos = [s["name"] for s in passos if s.get("name", "").startswith("⚠") and s.get("conclusion") == "success"]
    for n in avisos:                      # passo de aviso só RODA (sucesso) quando há o que avisar
        print("   " + n)
        if os.environ.get("GITHUB_ACTIONS"):
            print("::warning::" + n)
    if run.get("conclusion") == "success":
        print("✅ portão do backlog verde: banco, GAS e fumaça" + (" — COM AVISO acima" if avisos else ""))
        return 0
    print("❌ portão do backlog: %s" % (run.get("conclusion") or "?"))
    for n in ruins:
        print("   ❌ " + n)
        if os.environ.get("GITHUB_ACTIONS"):
            print("::error::portão do backlog reprovou: %s" % n)
    print("   detalhe (privado): " + url)
    return 1


def _autoteste_chamar(c):
    """Servidor falso da API do GitHub em 127.0.0.1: os casos que TÊM de reprovar e o verde."""
    import threading, http.server
    estado = dict(dispatch=204, conclusao="success", aparece=True, chamadas=[], aviso=False, titulo=None)
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass
        def _r(self, st, j=None):
            b = json.dumps(j or {}).encode(); self.send_response(st); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0); corpo = json.loads(self.rfile.read(n) or b"{}")
            estado["chamadas"].append((self.path, corpo, self.headers.get("Authorization")))
            self._r(estado["dispatch"], {} if estado["dispatch"] == 204 else {"message": "x"})
        def do_GET(self):
            ch = (estado["chamadas"][-1][1].get("inputs") or {}).get("chamada", "") if estado["chamadas"] else ""
            run = dict(id=7, status="completed", conclusion=estado["conclusao"], html_url="https://x/run/7",
                       display_title=estado["titulo"] if estado["titulo"] is not None else "portão GrupoTap/ProperAdmin#5 " + ch)
            if "/runs?" in self.path:
                self._r(200, dict(workflow_runs=[run] if estado["aparece"] else []))
            elif self.path.endswith("/jobs"):
                self._r(200, dict(jobs=[dict(steps=[dict(name="Toda rpc existe no banco", conclusion="failure" if estado["conclusao"] != "success" else "success"),
                                                    dict(name="⚠ Aviso: SQL da issue", conclusion="success" if estado["aviso"] else "skipped")])]))
            else:
                self._r(200, run)
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    env0 = dict(os.environ)
    os.environ.update(PORTAO_API="http://127.0.0.1:%d" % srv.server_port, REPO="GrupoTap/ProperAdmin", PR="5", SHA="a" * 40,
                      GITHUB_RUN_ID="99", GITHUB_RUN_ATTEMPT="1")
    os.environ.pop("GITHUB_ACTIONS", None)
    import io, contextlib
    def roda():
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            r = chamar_backlog(espera=0, teto=5, janela=1)
        return r, buf.getvalue()
    try:
        os.environ.pop("PORTAO_TOKEN", None)
        r, out = roda(); c("5-par sem o Secret PORTAO_TOKEN REPROVA (e diz o que fazer)", r == 1 and "PORTAO_TOKEN" in out, out)
        os.environ["PORTAO_TOKEN"] = "token-falso"
        r, out = roda()
        p, corpo, auth = estado["chamadas"][-1]
        c("chamada verde: dispara na main do backlog com repo/pr/sha e espera o ✅", r == 0 and corpo["ref"] == "main"
          and corpo["inputs"]["pr"] == "5" and p.endswith("/actions/workflows/portao.yml/dispatches") and auth == "Bearer token-falso", (r, out, corpo))
        c("o token nunca aparece na saída", "token-falso" not in out, out)
        estado["conclusao"] = "failure"
        r, out = roda(); c("5-par backlog vermelho REPROVA e diz o passo que reprovou", r == 1 and "Toda rpc existe no banco" in out, out)
        estado["conclusao"] = "success"; estado["dispatch"] = 401
        r, out = roda(); c("5-par token vencido (401) REPROVA com a dica", r == 1 and "venceu" in out, out)
        estado["dispatch"] = 204; estado["aparece"] = False
        r, out = roda(); c("5-par execução que não aparece REPROVA (não fica verde por omissão)", r == 1, out)
        estado["aparece"] = True; estado["titulo"] = "portão GrupoTap/ProperAdmin#5 x" + "a" * 12 + "-99-1-velha"
        r, out = roda(); c("B4-par execução de OUTRA chamada (nome só contém o id) não é casada (REPROVA)", r == 1, out)
        estado["titulo"] = None; estado["aviso"] = True
        r, out = roda(); c("M4: verde com passo de aviso traz o aviso para a PR pública", r == 0 and "⚠ Aviso: SQL da issue" in out and "COM AVISO" in out, out)
        estado["aviso"] = False
        os.environ["HEAD_REPO"] = "alguem/ProperAdmin"
        n0 = len(estado["chamadas"])
        r, out = roda(); c("B8-par PR de fork REPROVA sem chamar o backlog", r == 1 and "outro repositório" in out and len(estado["chamadas"]) == n0, out)
        os.environ.pop("HEAD_REPO", None)
    finally:
        srv.shutdown()
        os.environ.clear(); os.environ.update(env0)


# ── bancada da ferramenta (pares) ──────────────────────────────────────────────────────────────────────
def autoteste():
    tmp = tempfile.mkdtemp(prefix="portao_t_")
    casos = []
    def c(nome, cond, det=""):
        casos.append(bool(cond)); print(("  ✅ " if cond else "  ❌ ") + nome + (" — " + det if det and not cond else ""))
    def w(rel, txt):
        p = os.path.join(tmp, rel); os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w", encoding="utf-8").write(txt)
    def roda(*rels):
        a, v, _ = conferir(tmp, list(rels), falar=False)
        return a, v
    def regras(a):
        return sorted(x["regra"] for x in a)
    H = lambda *bs: "<!doctype html><html><body>\n" + "\n".join(bs) + "\n</body></html>\n"
    S = lambda c, t="": "<script%s>\n%s\n</script>" % ((' type="%s"' % t) if t else "", c)
    print(VERSAO + " — autoteste")
    # sintaxe
    w("ok.html", H(S("let a = 1;\nfunction f(){ return a; }"), S("const b = a + 1; f();")))
    a, _ = roda("ok.html"); c("1 arquivo bom passa", not a, str(a))
    w("quebrado.html", H(S("let a = 1;"), "<!-- texto -->", S("function g( { return 1; }")))
    a, _ = roda("quebrado.html"); c("1-par script quebrado REPROVA (sintaxe) na linha certa", regras(a) == ["sintaxe"] and a[0]["linha"] == 7, str(a))
    w("coment.html", H("<!-- <script>isto não é código {{{</script> -->", S("var x = '<script>nem isto</script>'.length;".replace("</script>", "<\\/script>"))))
    a, _ = roda("coment.html"); c("<script> citado em comentário HTML e em string NÃO vira bloco", not a, str(a))
    w("tipos.html", H(S('{"isto": "é json", }', "application/json"), S("<div>{{ template }}</div>", "text/template"), S("export const m = 1;", "module")))
    a, _ = roda("tipos.html"); c("json/template ficam fora; módulo é checado como módulo", not a, str(a))
    w("mod_ruim.html", H(S("export const m = ;", "module")))
    a, _ = roda("mod_ruim.html"); c("1-par módulo quebrado REPROVA", regras(a) == ["sintaxe"], str(a))
    w("sw.js", "self.addEventListener('fetch', e => {});\n")
    a, _ = roda("sw.js"); c(".js solto bom passa", not a, str(a))
    w("sw_ruim.js", "self.addEventListener('fetch', e => {;\n")
    a, _ = roda("sw_ruim.js"); c("1-par .js solto quebrado REPROVA", regras(a) == ["sintaxe"], str(a))
    # colisão
    w("col.html", H(S("let cfg = 1;"), S("let cfg = 2; console.log('nunca roda');")))
    a, _ = roda("col.html"); c("2-par let repetido entre blocos REPROVA (colisão)", regras(a) == ["colisao"] and a[0]["linha"] == 6, str(a))
    w("col2.html", H(S("var LISTA = [];"), S("const LISTA = [];")))
    a, _ = roda("col2.html"); c("2-par var × const entre blocos REPROVA", regras(a) == ["colisao"], str(a))
    w("col3.html", H(S("class Painel {}"), S("function Painel(){}")))
    a, _ = roda("col3.html"); c("2-par class × function entre blocos REPROVA", regras(a) == ["colisao"], str(a))
    w("col4.html", H(S("const { a, b: [c, ...d] } = {b: []};"), S("let d = 0;")))
    a, _ = roda("col4.html"); c("2-par nome de desestruturação repetido REPROVA", regras(a) == ["colisao"], str(a))
    w("col_ok.html", H(S("var v = 1; var v2 = 2;"), S("var v = 3; window.cfg = 1;"), S("let cfg = 2; { let interno = 1; } function h(){ let v = 9; }")))
    a, _ = roda("col_ok.html"); c("var repetido, propriedade de window e let interno NÃO acusam", not a, str(a))
    w("col_mod.html", H(S("let m1 = 1;"), S("let m1 = 2;", "module")))
    a, _ = roda("col_mod.html"); c("módulo tem escopo próprio: let do módulo não colide", not a, str(a))
    # função 2×
    w("f2.html", H(S("function pinta(){ return 1; }"), S("function pinta(){ return 2; }")))
    a, _ = roda("f2.html"); c("3-par função declarada 2× SEM marca REPROVA", regras(a) == ["funcao2x"], str(a))
    w("f2m.html", H(S("function pinta(){ return 1; }"), S("// V190: a fila nova manda\n// ⚠ SOBRESCREVE pinta\nfunction pinta(){ return 2; }")))
    a, _ = roda("f2m.html"); c("função 2× COM a marca passa", not a, str(a))
    w("f2x.html", H(S("function pinta(){ return 1; }"), S("// ⚠ SOBRESCREVE outra\nfunction pinta(){ return 2; }")))
    a, _ = roda("f2x.html"); c("3-par marca com o NOME ERRADO não vale (REPROVA)", regras(a) == ["funcao2x"], str(a))
    w("f2longe.html", H(S("function pinta(){ return 1; }"), S("// ⚠ SOBRESCREVE pinta\n\n\n\n\nfunction pinta(){ return 2; }")))
    a, _ = roda("f2longe.html"); c("3-par marca longe (5 linhas acima) não vale (REPROVA)", regras(a) == ["funcao2x"], str(a))
    w("f2mesmo.html", H(S("function pinta(){ return 1; }\nfunction pinta(){ return 2; }")))
    a, _ = roda("f2mesmo.html"); c("3-par função 2× no MESMO bloco REPROVA", regras(a) == ["funcao2x"], str(a))
    # src local
    w("lib/comum.js", "let COMUM = 1;\n")
    w("src.html", H('<script src="lib/comum.js"></script>', S("let COMUM = 2;")))
    a, _ = roda("src.html"); c("4-par <script src> local entra no escopo (colisão com ele REPROVA)", regras(a) == ["colisao"], str(a))
    w("src_ext.html", H('<script src="https://cdn.exemplo/x.js"></script>', '<script src="jspdf.umd.min.js"></script>', S("let ok = 1;")))
    a, _ = roda("src_ext.html"); c("src externo e biblioteca minificada ficam fora", not a, str(a))
    # listar
    w("vendor/pesado.js", "isto não é js {{{\n")
    w("x.min.js", "{{{\n")
    ls = listar(tmp, "tudo")
    c("--tudo pula vendor/ e *.min.js", "vendor/pesado.js" not in ls and "x.min.js" not in ls and "ok.html" in ls, str(ls))
    # repo git: --base só pega o que mudou
    g = os.path.join(tmp, "repo"); os.makedirs(g)
    run = lambda *a: subprocess.run(["git", "-C", g] + list(a), capture_output=True, text=True)
    run("init", "-q", "-b", "main"); run("config", "user.email", "t@t"); run("config", "user.name", "t")
    open(os.path.join(g, "a.html"), "w").write(H(S("let a = 1;")))
    open(os.path.join(g, "b.html"), "w").write(H(S("let b = 1;")))
    run("add", "."); run("commit", "-qm", "base")
    run("checkout", "-qb", "pr")
    open(os.path.join(g, "b.html"), "w").write(H(S("let b = 1;"), S("let b = 2;")))
    run("commit", "-qam", "pr")
    c("--base lista só o que a PR mudou", listar(g, "base", "main") == ["b.html"], str(listar(g, "base", "main")))
    # achados da auditoria de 01/11 (cada um com o par que TEM de reprovar)
    for nome, js in (("ret", "if (window.x) return;"), ("exp", "export const a = 1;"), ("tla", "await fetch('/x');")):
        w("a2_%s.html" % nome, H(S(js)))
        a, _ = roda("a2_%s.html" % nome); c("A2-par `%s` no topo de script clássico REPROVA (o node aceita, o navegador não)" % js, regras(a) == ["sintaxe"], str(a))
    w("a2_js.js", "export default 1;\n")
    a, _ = roda("a2_js.js"); c("A2-par .js solto com export (carregado como script) REPROVA", regras(a) == ["sintaxe"], str(a))
    for nome, js in (("for", "for (var i = 0; i < 2; i++) {}"), ("if", "if (true) { var i = 1; }"), ("try", "try { var i = 1; } catch (e) {}"),
                     ("sw", "switch (1) { case 1: var i = 1; }")):
        w("a3_%s.html" % nome, H(S("let i = 0;"), S(js)))
        a, _ = roda("a3_%s.html" % nome); c("A3-par let × var aninhado (%s) entre blocos REPROVA" % nome, regras(a) == ["colisao"], str(a))
    w("a3_ok.html", H(S("let i = 0;"), S("function f(){ var i = 1; } { let i = 2; } (() => { var i = 3; })();")))
    a, _ = roda("a3_ok.html"); c("var dentro de função e let de bloco NÃO colidem", not a, str(a))
    w("app.js", "export const a = 1;\n"); w("usa_mod.html", H('<script type="module" src="app.js"></script>'))
    a, _ = roda("app.js"); c(".js carregado como type=module é lido como módulo (export não acusa)", not a, str(a))
    w("fora.html", H('<script src="../fora.js"></script>'))
    a, _ = roda("fora.html"); c("src que sai do repositório (../) REPROVA", regras(a) == ["sintaxe"] and "FORA" in a[0]["msg"], str(a))
    w("m2.html", "<html><body>\n<script>\nlet a = 1;\n</script>\n<script>\nlet b = (;\n")
    a, _ = roda("m2.html"); c("M2-par <script> sem </script> (arquivo truncado) REPROVA", "sintaxe" in regras(a), str(a))
    w("m1_falta.html", H('<script src="nao_existe.js"></script>', S("let ok = 1;")))
    a, _ = roda("m1_falta.html"); c("M1-par <script src> local que não existe REPROVA", regras(a) == ["sintaxe"], str(a))
    w("b5.html", H("<template><script>let t = 1;</script></template>", "<noscript><script>let t = 2;</script></noscript>",
                   "<textarea><script>let t = 3;</script></textarea>", S("let t = 4;")))
    a, _ = roda("b5.html"); c("<script> dentro de template/noscript/textarea não roda (não colide)", not a, str(a))
    w("b5_tipo.html", H(S("let x = (;", "text/x-javascript")))
    a, _ = roda("b5_tipo.html"); c("B5-par tipo legado text/x-javascript é conferido (quebrado REPROVA)", regras(a) == ["sintaxe"], str(a))
    # --base: .js mudado/apagado puxa o .html que o carrega; nome com acento
    g2 = os.path.join(tmp, "repo2"); os.makedirs(g2)
    run = lambda *a: subprocess.run(["git", "-C", g2] + list(a), capture_output=True, text=True)
    run("init", "-q", "-b", "main"); run("config", "user.email", "t@t"); run("config", "user.name", "t")
    open(os.path.join(g2, "pwa.js"), "w").write("var PWA = 1;\n")
    open(os.path.join(g2, "outro.js"), "w").write("var OUTRO = 1;\n")
    open(os.path.join(g2, "index.html"), "w").write(H('<script src="pwa.js"></script>', '<script src="outro.js"></script>', S("let GAS_URL = 1;")))
    open(os.path.join(g2, "relatório.html"), "w").write(H(S("let r = 1;")))
    run("add", "."); run("commit", "-qm", "base")
    run("checkout", "-qb", "pr1")
    open(os.path.join(g2, "pwa.js"), "w").write("var PWA = 1;\nconst GAS_URL = 2;\n")
    run("commit", "-qam", "pr1")
    ls = listar(g2, "base", "main")
    a, _, _ = conferir(g2, ls, falar=False)
    c("M1-par .js mudado puxa o .html que o carrega e a colisão com ele REPROVA", "index.html" in ls and regras(a) == ["colisao"], str((ls, a)))
    run("checkout", "-q", "main"); run("checkout", "-qb", "pr2"); run("rm", "-q", "outro.js"); run("commit", "-qm", "pr2")
    ls = listar(g2, "base", "main")
    a, _, _ = conferir(g2, ls, falar=False)
    c("M1-par .js apagado puxa o .html que o carregava (src inexistente REPROVA)", ls == ["index.html"] and regras(a) == ["sintaxe"], str((ls, a)))
    run("checkout", "-q", "main"); run("checkout", "-qb", "pr3")
    open(os.path.join(g2, "relatório.html"), "w").write(H(S("let r = (;")))
    run("commit", "-qam", "pr3")
    ls = listar(g2, "base", "main")
    a, _, _ = conferir(g2, ls, falar=False)
    c("B1-par .html com acento no nome entra na lista (quebrado REPROVA)", ls == ["relatório.html"] and regras(a) == ["sintaxe"], str((ls, a)))
    _autoteste_chamar(c)
    shutil.rmtree(tmp, ignore_errors=True)
    print("AUTOTESTE: %d/%d" % (sum(casos), len(casos)))
    return 0 if all(casos) else 1


def main(a):
    if "--autoteste" in a:
        return autoteste()
    if "--chamar-backlog" in a:
        return chamar_backlog()
    opt = lambda n: a[a.index(n) + 1] if n in a else None
    raiz = os.path.abspath(opt("--raiz") or ".")
    soltos = [x for i, x in enumerate(a) if not x.startswith("--") and (i == 0 or a[i - 1] not in ("--raiz", "--base", "--json"))]
    if "--base" in a:
        rels = listar(raiz, "base", opt("--base"))
    elif "--tudo" in a:
        rels = listar(raiz, "tudo")
    elif soltos:
        rels = listar(raiz, "arq", arquivos=soltos)
    else:
        print(__doc__); return 9
    ach, av, res = conferir(raiz, rels)
    if opt("--json"):
        with open(opt("--json"), "w", encoding="utf-8") as fh:
            json.dump(dict(versao=VERSAO, arquivos=rels, achados=ach, avisos=av, resumo=res), fh, ensure_ascii=False, indent=1)
    return 1 if ach else 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit as e:
        if e.code == 9 and not shutil.which("node"):
            print("🔴 node não está instalado — o portão precisa do node para o `node --check`")
        raise
