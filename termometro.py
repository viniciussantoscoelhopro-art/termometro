"""
Termometro de regime - Exhaustion EA
Calcula, uma vez por dia, se cada par esta num regime de tendencia forte.
Saida: termometro.csv  (par;tendencia;nota;er_pct;juros_pct;cot_pct;vix_pct;data;vix_nivel)

Componentes (cada um vira um percentil 0-1 contra a propria historia):
  er_pct    persistencia de tendencia do par (efficiency ratio 60 dias)
  juros_pct velocidade da divergencia de juros entre as duas economias (6 meses)
  cot_pct   velocidade da mudanca de posicionamento especulativo (CFTC, 13 semanas)
  vix_pct   estresse global (VIX, media de 20 dias) -- e o unico que o EA usa para bloquear
  vix_nivel o mesmo VIX em pontos (media de 20 dias), so para leitura humana
nota = quantos componentes estao no extremo (>= 0.90). Fonte que falhar fica vazia e nao conta.
tendencia = +1 se o fechamento esta acima da media de 100 dias, -1 se abaixo.
"""
import csv, io, os, sys, json, zipfile, datetime as dt, statistics as st, urllib.request

PARES = ["AUDUSD","EURCHF","GBPNZD","CADJPY","EURAUD","AUDCAD","GBPCAD","EURNZD"]
EXTREMO = 0.90
HOJE = dt.date.today()

FRED_KEY = os.environ.get("FRED_API_KEY", "").strip()

def baixar(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "termometro-regime/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def fred(serie):
    """Serie do FRED pela API oficial (exige chave gratuita em FRED_API_KEY).
    O link de download do site (fredgraph.csv) bloqueia servidores de nuvem como o GitHub Actions."""
    if not FRED_KEY:
        raise RuntimeError("FRED_API_KEY nao configurada")
    url = (f"https://api.stlouisfed.org/fred/series/observations?series_id={serie}"
           f"&api_key={FRED_KEY}&file_type=json&observation_start=1990-01-01")
    dados = json.loads(baixar(url))
    out = []
    for o in dados.get("observations", []):
        try: out.append((dt.date.fromisoformat(o["date"]), float(o["value"])))
        except ValueError: pass          # FRED usa "." para dia sem dado
    return out

# reserva para o cambio: espelho publico dos mesmos dados do Fed (H.10), hospedado no proprio GitHub
ESPELHO_FX = "https://raw.githubusercontent.com/datasets/exchange-rates/main/data/daily.csv"
PAIS_ESPELHO = {"JPY": "Japan", "CAD": "Canada", "CHF": "Switzerland", "AUD": "Australia",
                "EUR": "Euro", "NZD": "New Zealand", "GBP": "United Kingdom"}

def fx_espelho():
    """Todas as moedas em 'unidades por dolar' -> convertemos para 'dolares por unidade'."""
    txt = baixar(ESPELHO_FX, 90).decode()
    por_pais = {p: m for m, p in PAIS_ESPELHO.items()}
    usd = {m: {} for m in PAIS_ESPELHO}
    for r in csv.DictReader(io.StringIO(txt)):
        m = por_pais.get(r["Country"])
        if not m: continue
        try:
            v = float(r["Exchange rate"]); d = dt.date.fromisoformat(r["Date"])
            if v > 0 and d.year >= 1990: usd[m][d] = 1 / v
        except ValueError: pass
    return usd

def percentil(atual, historico):
    h = [x for x in historico if x is not None]
    if atual is None or len(h) < 30: return None
    return sum(1 for x in h if x <= atual) / len(h)

# ---------------------------------------------------------------- CAMBIO (FRED)
# unidades por dolar -> convertemos tudo para "dolares por 1 unidade da moeda"
FX = {"JPY": ("DEXJPUS", True), "CAD": ("DEXCAUS", True), "CHF": ("DEXSZUS", True),
      "AUD": ("DEXUSAL", False), "EUR": ("DEXUSEU", False), "NZD": ("DEXUSNZ", False),
      "GBP": ("DEXUSUK", False)}

def carregar_fx():
    usd = {}
    for moeda, (serie, inverte) in FX.items():
        try:
            usd[moeda] = {d: (1/v if inverte else v) for d, v in fred(serie) if v > 0}
        except Exception as e:
            print(f"[aviso] cambio {moeda} pelo FRED falhou: {e}")
    faltando = [m for m in FX if not usd.get(m)]
    if faltando:
        try:
            esp = fx_espelho()
            for m in faltando:
                if esp.get(m): usd[m] = esp[m]; print(f"[info] cambio {m}: usando espelho do GitHub")
        except Exception as e:
            print(f"[aviso] espelho de cambio tambem falhou: {e}")
    return usd

def serie_par(usd, par):
    b, q = par[:3], par[3:]
    get = lambda m, d: 1.0 if m == "USD" else usd.get(m, {}).get(d)
    datas = sorted(set(usd.get(b, {}) if b != "USD" else usd.get(q, {})) &
                   set(usd.get(q, {}) if q != "USD" else usd.get(b, {})))
    return [(d, get(b, d) / get(q, d)) for d in datas if get(b, d) and get(q, d)]

def efficiency_ratio(px, n=60):
    if len(px) <= n: return None
    liquido = abs(px[-1] - px[-1-n])
    caminho = sum(abs(px[i] - px[i-1]) for i in range(len(px)-n, len(px)))
    return liquido / caminho if caminho > 0 else None

# ---------------------------------------------------------------- JUROS (FRED/OECD)
# taxa de curtissimo prazo (overnight/politica). Mensal, com 1-2 meses de atraso.
JUROS = {"USD": ["DFF", "IRSTCI01USM156N"], "EUR": ["ECBDFR", "IRSTCI01EZM156N"],
         "JPY": ["IRSTCI01JPM156N"], "GBP": ["IRSTCI01GBM156N"], "CHF": ["IRSTCI01CHM156N"],
         "AUD": ["IRSTCI01AUM156N"], "CAD": ["IRSTCI01CAM156N"], "NZD": ["IRSTCI01NZM156N"]}

def carregar_juros():
    out = {}
    for moeda, series in JUROS.items():
        for s in series:
            try:
                dados = fred(s)
                if dados and (HOJE - dados[-1][0]).days < 200:
                    mensal = {}
                    for d, v in dados: mensal[(d.year, d.month)] = v   # ultimo valor de cada mes
                    out[moeda] = mensal; break
            except Exception as e:
                print(f"[aviso] juros {moeda} via {s}: {e}")
        if moeda not in out: print(f"[aviso] juros {moeda} indisponivel")
    return out

def juros_componente(juros, par, ate):
    b, q = par[:3], par[3:]
    if b not in juros or q not in juros: return None
    meses = sorted(k for k in set(juros[b]) & set(juros[q]) if k <= (ate.year, ate.month))
    if len(meses) < 60: return None
    dif = [juros[b][m] - juros[q][m] for m in meses]
    vel = [abs(dif[i] - dif[i-6]) for i in range(6, len(dif))]
    return percentil(vel[-1], vel[-240:])

# ---------------------------------------------------------------- COT (CFTC)
COT = {"JPY": "097741", "CAD": "090741", "AUD": "232741", "NZD": "112741",
       "GBP": "096742", "CHF": "092741", "EUR": "099741"}

def carregar_cot(anos=10):
    pos = {m: {} for m in COT}
    for ano in range(HOJE.year - anos, HOJE.year + 1):
        try:
            z = zipfile.ZipFile(io.BytesIO(baixar(f"https://www.cftc.gov/files/dea/history/deacot{ano}.zip", 120)))
            txt = z.read(z.namelist()[0]).decode("latin-1")
        except Exception as e:
            print(f"[aviso] COT {ano}: {e}"); continue
        leitor = csv.DictReader(io.StringIO(txt))
        col = lambda frag: next((c for c in leitor.fieldnames if frag in c), None)
        c_cod, c_data = col("Contract Market Code"), col("YYYY-MM-DD")
        c_oi, c_l, c_s = col("Open Interest (All)"), col("Noncommercial Positions-Long (All)"), col("Noncommercial Positions-Short (All)")
        if not all([c_cod, c_data, c_oi, c_l, c_s]): print(f"[aviso] COT {ano}: colunas nao reconhecidas"); continue
        for r in leitor:
            for moeda, cod in COT.items():
                if r[c_cod].strip() == cod:
                    try:
                        oi = float(r[c_oi]); d = dt.date.fromisoformat(r[c_data].strip())
                        pos[moeda][d] = (float(r[c_l]) - float(r[c_s])) / oi if oi > 0 else 0.0
                    except ValueError: pass
    return pos

def cot_componente(cot, par, ate):
    b, q = par[:3], par[3:]
    pega = lambda m: {} if m == "USD" else cot.get(m, {})
    pb, pq = pega(b), pega(q)
    if (b != "USD" and not pb) or (q != "USD" and not pq): return None
    datas = sorted(d for d in (set(pb) | set(pq)) if d <= ate and (b == "USD" or d in pb) and (q == "USD" or d in pq))
    if len(datas) < 120: return None
    liq = [(pb.get(d, 0.0) if b != "USD" else 0.0) - (pq.get(d, 0.0) if q != "USD" else 0.0) for d in datas]
    vel = [abs(liq[i] - liq[i-13]) for i in range(13, len(liq))]
    return percentil(vel[-1], vel[-520:])

# ---------------------------------------------------------------- VIX (FRED)
def vix_componente(vix, ate):
    """Retorna (percentil da media de 20 dias contra os ultimos 10 anos, media de 20 dias em pontos)."""
    v = [x for d, x in vix if d <= ate]
    if len(v) < 300: return None, None
    med20 = [st.mean(v[i-20:i]) for i in range(20, len(v)+1)]
    return percentil(med20[-1], med20[-2520:]), med20[-1]

# ---------------------------------------------------------------- TABELA
def tabela(usd, juros, cot, vix, ate):
    linhas = []
    pv, nv = vix_componente(vix, ate) if vix else (None, None)
    for par in PARES:
        s = [(d, p) for d, p in serie_par(usd, par) if d <= ate]
        if len(s) < 300:
            linhas.append([par, 0, 0, None, None, None, pv, nv]); continue
        px = [p for _, p in s]
        tend = 1 if px[-1] > st.mean(px[-100:]) else -1
        ers = [efficiency_ratio(px[:i]) for i in range(max(61, len(px)-2520), len(px)+1, 5)]
        per = percentil(ers[-1], ers)
        pj = juros_componente(juros, par, ate)
        pc = cot_componente(cot, par, ate)
        comps = [per, pj, pc, pv]
        nota = sum(1 for c in comps if c is not None and c >= EXTREMO)
        linhas.append([par, tend, nota, per, pj, pc, pv, nv])
    return linhas

def fmt(x): return "" if x is None else f"{x:.2f}"
def fmt1(x): return "" if x is None else f"{x:.1f}"

def main():
    usd = carregar_fx()
    juros = carregar_juros()
    cot = carregar_cot()
    try: vix = fred("VIXCLS")
    except Exception as e: print(f"[aviso] VIX: {e}"); vix = []
    validos = [s for s in usd.values() if s]
    if not validos:
        print("[erro] nenhum dado de cambio disponivel (FRED e espelho falharam). Tabela nao atualizada.")
        sys.exit(1)
    ultimo = max(max(s) for s in validos)
    print(f"[info] fontes: cambio {len(validos)}/7 | juros {len(juros)}/8 | COT {sum(1 for v in cot.values() if v)}/7 | VIX {'ok' if vix else 'falhou'}")
    with open("termometro.csv", "w") as f:
        f.write("par;tendencia;nota;er_pct;juros_pct;cot_pct;vix_pct;data;vix_nivel\n")
        for l in tabela(usd, juros, cot, vix, ultimo):
            # data = dia do CALCULO (o EA usa para saber se a tabela esta atualizada).
            # O cambio do Fed sai semanalmente, entao a ultima cotacao pode ter ate ~10 dias.
            f.write(f"{l[0]};{l[1]};{l[2]};{fmt(l[3])};{fmt(l[4])};{fmt(l[5])};{fmt(l[6])};{HOJE.isoformat()};{fmt1(l[7])}\n")
    print(f"ultima cotacao usada: {ultimo}")
    print(open("termometro.csv").read())
    if "--historico" in sys.argv:           # tabela semanal desde 2018, para a validacao
        with open("termometro_historico.csv", "w") as f:
            f.write("data;par;tendencia;nota;er_pct;juros_pct;cot_pct;vix_pct;vix_nivel\n")
            d = dt.date(2018, 1, 5)
            while d <= ultimo:
                for l in tabela(usd, juros, cot, vix, d):
                    f.write(f"{d.isoformat()};{l[0]};{l[1]};{l[2]};{fmt(l[3])};{fmt(l[4])};{fmt(l[5])};{fmt(l[6])};{fmt1(l[7])}\n")
                d += dt.timedelta(days=7)
        print("historico gravado")

if __name__ == "__main__":
    main()
