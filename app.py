"""
Veille Journal Officiel — Environnement & CEE
================================================

Application Streamlit qui récupère les textes publiés au Journal Officiel
et les résume via Claude, avec un filtrage particulier sur les sujets
environnementaux et les Certificats d'Économies d'Énergie (CEE).

Installation :
  pip install streamlit requests anthropic --break-system-packages

Configuration des secrets (fichier .streamlit/secrets.toml en local,
ou "Secrets" dans Streamlit Community Cloud) :

  PISTE_CLIENT_ID = "votre_client_id"
  PISTE_CLIENT_SECRET = "votre_client_secret"
  ANTHROPIC_API_KEY = "votre_cle_anthropic"

Lancement local :
  streamlit run app.py
"""

import json
from datetime import date, timedelta

import requests
import streamlit as st
from anthropic import Anthropic

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PISTE_TOKEN_URL = "https://oauth.piste.gouv.fr/api/oauth/token"
LEGIFRANCE_BASE_URL = "https://api.piste.gouv.fr/dila/legifrance/lf-engine-app"

# Mots-clés utilisés pour cibler le fonds environnement / CEE.
# Le filtre s'applique en local sur les titres/contenus récupérés, en
# complément d'une éventuelle recherche par mot-clé côté API.
KEYWORDS_CEE = [
    "certificat d'économies d'énergie",
    "certificats d'économies d'énergie",
    "CEE",
    "économie d'énergie",
    "économies d'énergie",
    "efficacité énergétique",
]

KEYWORDS_ENVIRONNEMENT = [
    "environnement",
    "biodiversité",
    "climat",
    "pollution",
    "déchets",
    "énergie renouvelable",
    "transition énergétique",
    "eau",
    "émissions",
    "carbone",
] + KEYWORDS_CEE

st.set_page_config(page_title="Veille JO — Environnement & CEE", page_icon="🌱", layout="wide")


# ---------------------------------------------------------------------------
# Authentification PISTE
# ---------------------------------------------------------------------------

@st.cache_data(ttl=3000, show_spinner=False)
def get_piste_token(client_id: str, client_secret: str) -> str:
    response = requests.post(
        PISTE_TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "openid",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["access_token"]


# ---------------------------------------------------------------------------
# Récupération des textes du JO
# ---------------------------------------------------------------------------

def fetch_jo_texts(token: str, target_date: str) -> list[dict]:
    """
    Récupère les textes publiés au JO à une date donnée (fonds JORF).

    D'après la doc officielle Légifrance/PISTE : /consult/jorf sert à
    récupérer UN texte précis via son identifiant JORFTEXT — il ne prend
    pas de date en entrée (c'est ce qui causait l'erreur 500 initiale).
    Pour lister les textes d'un jour donné, il faut passer par
    /consult/jorfCont, qui renvoie le "conteneur" du Journal Officiel de
    ce jour avec la structure (sommaire) des textes qu'il contient.

    NOTE : le nom exact des champs de payload/réponse peut varier selon la
    version de l'API — à valider avec le Swagger disponible dans ton
    espace PISTE (onglet "Explorer" / Swagger 2.0) si besoin d'ajuster.
    """
    url = f"{LEGIFRANCE_BASE_URL}/consult/jorfCont"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {"date": target_date}

    response = requests.post(url, headers=headers, json=payload, timeout=30)
    if response.status_code != 200:
        st.error(f"Erreur API Légifrance jorfCont ({response.status_code}) : {response.text[:300]}")
        return []

    data = response.json()

    # La réponse contient normalement une "structure" ou un "sommaire"
    # listant chaque texte (id JORFTEXT, titre, nature...). Le nom exact
    # du champ dépend de la version de l'API — on tente plusieurs clés.
    structure = (
        data.get("structure")
        or data.get("sommaire")
        or data.get("textes")
        or data.get("items")
        or []
    )

    # Les textes peuvent être imbriqués sous des sections selon la
    # structure retournée. On aplatit tout en une liste simple.
    textes: list[dict] = []

    def _extract(node):
        if isinstance(node, dict):
            if "titre" in node or "id" in node:
                textes.append(node)
            for value in node.values():
                _extract(value)
        elif isinstance(node, list):
            for item in node:
                _extract(item)

    _extract(structure)
    return textes


def search_jo_by_keyword(token: str, keyword: str, target_date: str, nb_results: int = 20) -> list[dict]:
    """
    Recherche des textes JORF contenant un mot-clé précis, à une date donnée.
    Utilise l'endpoint de recherche générale de Légifrance (/search),
    restreint au fonds JORF. À ajuster selon la doc PISTE si le format
    de payload attendu diffère.
    """
    url = f"{LEGIFRANCE_BASE_URL}/search"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "recherche": {
            "champs": [
                {
                    "typeChamp": "ALL",
                    "criteres": [{"typeRecherche": "UN_DES_MOTS", "valeur": keyword, "operateur": "ET"}],
                    "operateur": "ET",
                }
            ],
            "filtres": [
                {"facette": "DATE_SIGNATURE", "dates": {"start": target_date, "end": target_date}},
            ],
            "pageSize": nb_results,
            "pageNumber": 1,
            "sort": "PERTINENCE",
            "typePagination": "DEFAUT",
        },
        "fond": "JORF",
    }

    response = requests.post(url, headers=headers, json=payload, timeout=30)
    if response.status_code != 200:
        st.warning(f"Recherche par mot-clé indisponible ({response.status_code}). Filtrage local utilisé à la place.")
        return []

    data = response.json()
    return data.get("results", data.get("textes", []))


def matches_environnement(texte: dict) -> bool:
    """Filtre local : le texte est-il probablement lié à l'environnement/CEE ?"""
    contenu = " ".join([
        str(texte.get("titre", "")),
        str(texte.get("contenu", "")),
        str(texte.get("resume", "")),
        str(texte.get("nature", "")),
    ]).lower()
    return any(kw.lower() in contenu for kw in KEYWORDS_ENVIRONNEMENT)


def is_cee_specific(texte: dict) -> bool:
    """Filtre local plus strict : spécifique aux CEE."""
    contenu = " ".join([
        str(texte.get("titre", "")),
        str(texte.get("contenu", "")),
        str(texte.get("resume", "")),
    ]).lower()
    return any(kw.lower() in contenu for kw in KEYWORDS_CEE)


# ---------------------------------------------------------------------------
# Résumé via Claude
# ---------------------------------------------------------------------------

def summarize_text(client: Anthropic, texte: dict, focus_cee: bool = False) -> str:
    titre = texte.get("titre", "Titre inconnu")
    nature = texte.get("nature", "")
    contenu = texte.get("contenu") or texte.get("resume") or titre

    focus_instruction = (
        "Ce texte concerne potentiellement les Certificats d'Économies d'Énergie (CEE). "
        "Précise si c'est le cas, et si oui, quel dispositif ou quelle fiche CEE est concerné(e) "
        "(ex : création/modification d'une fiche standardisée, évolution de coefficient, "
        "obligation des fournisseurs d'énergie, etc.)."
        if focus_cee else ""
    )

    prompt = f"""Voici un texte publié au Journal Officiel français.

Titre : {titre}
Nature : {nature}
Contenu : {contenu}

Rédige un résumé court et structuré en français, destiné à un public professionnel
du secteur de l'énergie/environnement :
1. Objet du texte (1 phrase)
2. Ce qui change concrètement (2-3 points max)
3. Date d'entrée en vigueur si mentionnée
4. Qui est concerné (entreprises, particuliers, obligés CEE, etc.)

{focus_instruction}

Reste factuel, ne fais pas d'interprétation juridique, et précise si une
information n'est pas disponible dans le texte fourni."""

    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


# ---------------------------------------------------------------------------
# Interface Streamlit
# ---------------------------------------------------------------------------

def main():
    st.title("🌱 Veille Journal Officiel — Environnement & CEE")
    st.caption(
        "Récupère les textes du Journal Officiel et met en avant ceux liés à "
        "l'environnement, avec un focus particulier sur les Certificats "
        "d'Économies d'Énergie (CEE)."
    )

    # --- Vérification des secrets ---
    # PISTE est obligatoire (sans ça, impossible de récupérer les textes).
    # ANTHROPIC_API_KEY est optionnelle : si absente, l'app affiche les
    # textes bruts sans résumé, ce qui permet de valider la récupération
    # côté Légifrance avant d'activer la couche IA.
    missing_required = [
        k for k in ["PISTE_CLIENT_ID", "PISTE_CLIENT_SECRET"]
        if k not in st.secrets
    ]
    if missing_required:
        st.error(
            "Secrets manquants : " + ", ".join(missing_required) +
            ".\n\nAjoute-les dans `.streamlit/secrets.toml` en local, ou dans "
            "les Secrets de ton app sur Streamlit Community Cloud."
        )
        st.code(
            'PISTE_CLIENT_ID = "..."\nPISTE_CLIENT_SECRET = "..."\nANTHROPIC_API_KEY = "..."  # optionnel',
            language="toml",
        )
        st.stop()

    has_anthropic_key = "ANTHROPIC_API_KEY" in st.secrets
    if not has_anthropic_key:
        st.warning(
            "⚠️ Clé ANTHROPIC_API_KEY absente : les textes seront affichés "
            "sans résumé automatique. Ajoute-la dans les Secrets pour activer "
            "les résumés IA."
        )

    # --- Barre latérale : paramètres ---
    with st.sidebar:
        st.header("Paramètres")
        date_range = st.date_input(
            "Plage de dates du JO",
            value=(date.today() - timedelta(days=1), date.today()),
            max_value=date.today(),
        )
        mode = st.radio(
            "Mode de récupération",
            ["Tous les textes du jour (filtrés ensuite)", "Recherche ciblée CEE / environnement"],
            index=1,
        )
        only_environnement = st.checkbox("N'afficher que les textes liés à l'environnement", value=True)
        cee_focus = st.checkbox("Focus spécifique CEE dans les résumés", value=True)
        run = st.button("🔍 Lancer la récupération", type="primary", use_container_width=True)

    if not run:
        st.info("Choisis une plage de dates et une méthode de récupération dans la barre latérale, puis lance la recherche.")
        return

    # date_input renvoie un tuple (début, fin) en mode plage, ou une date
    # seule si l'utilisateur n'a encore sélectionné qu'un seul jour.
    if isinstance(date_range, tuple) and len(date_range) == 2:
        start_date, end_date = date_range
    else:
        start_date = end_date = date_range if not isinstance(date_range, tuple) else date_range[0]

    if start_date > end_date:
        st.error("La date de début doit précéder la date de fin.")
        st.stop()

    nb_days = (end_date - start_date).days + 1
    if nb_days > 31:
        st.warning(
            f"Plage de {nb_days} jours sélectionnée — ça peut représenter beaucoup "
            "d'appels API et de temps de traitement. Envisage de réduire la période."
        )

    with st.spinner("Authentification auprès de PISTE..."):
        try:
            token = get_piste_token(st.secrets["PISTE_CLIENT_ID"], st.secrets["PISTE_CLIENT_SECRET"])
        except Exception as e:
            st.error(f"Échec de l'authentification PISTE : {e}")
            st.stop()

    # --- Récupération des textes sur toute la plage ---
    textes = []
    seen_ids_global = set()
    dates_a_traiter = [start_date + timedelta(days=i) for i in range(nb_days)]

    with st.spinner(f"Récupération des textes du {start_date.isoformat()} au {end_date.isoformat()}..."):
        progress_fetch = st.progress(0.0)
        for day_idx, current_date in enumerate(dates_a_traiter):
            current_date_str = current_date.isoformat()

            if mode.startswith("Recherche ciblée"):
                jour_textes = []
                for kw in KEYWORDS_ENVIRONNEMENT:
                    results = search_jo_by_keyword(token, kw, current_date_str)
                    for r in results:
                        rid = r.get("id") or r.get("titre")
                        if rid not in seen_ids_global:
                            seen_ids_global.add(rid)
                            r["_date"] = current_date_str
                            jour_textes.append(r)
                # Si la recherche par mot-clé ne renvoie rien pour ce jour
                # (endpoint à ajuster), on retombe sur récupération + filtre local.
                if not jour_textes:
                    day_all = fetch_jo_texts(token, current_date_str)
                    if only_environnement:
                        day_all = [t for t in day_all if matches_environnement(t)]
                    for t in day_all:
                        t["_date"] = current_date_str
                    jour_textes = day_all
                textes.extend(jour_textes)
            else:
                day_all = fetch_jo_texts(token, current_date_str)
                if only_environnement:
                    day_all = [t for t in day_all if matches_environnement(t)]
                for t in day_all:
                    t["_date"] = current_date_str
                textes.extend(day_all)

            progress_fetch.progress((day_idx + 1) / nb_days)
        progress_fetch.empty()

    if not textes:
        st.warning(
            "Aucun texte trouvé sur cette plage avec ces critères. "
            "Essaie une autre période, ou vérifie l'endpoint API dans le code "
            "(la structure de réponse PISTE peut nécessiter un ajustement)."
        )
        return

    st.success(f"{len(textes)} texte(s) trouvé(s) sur {nb_days} jour(s).")

    # --- Résumé + affichage ---
    anthropic_client = Anthropic(api_key=st.secrets["ANTHROPIC_API_KEY"]) if has_anthropic_key else None
    resultats = []

    progress = st.progress(0.0)
    for i, texte in enumerate(textes):
        titre = texte.get("titre", "Sans titre")
        is_cee = is_cee_specific(texte)

        if has_anthropic_key:
            with st.spinner(f"Résumé en cours : {titre[:60]}..."):
                try:
                    resume = summarize_text(anthropic_client, texte, focus_cee=(cee_focus and is_cee))
                except Exception as e:
                    resume = f"⚠️ Erreur lors du résumé : {e}"
        else:
            # Pas de clé Anthropic : on affiche le contenu brut disponible
            # (contenu complet, ou résumé fourni par l'API, ou titre à défaut).
            resume = texte.get("contenu") or texte.get("resume") or "(Aucun contenu brut disponible dans la réponse API pour ce texte.)"

        resultats.append({
            "date": texte.get("_date", ""),
            "titre": titre,
            "nature": texte.get("nature", ""),
            "cee": is_cee,
            "resume": resume,
        })
        progress.progress((i + 1) / len(textes))

    progress.empty()

    # Tri : textes CEE en premier
    resultats.sort(key=lambda r: not r["cee"])

    # Tri : date d'abord (plus récent en premier), puis textes CEE en tête
    resultats.sort(key=lambda r: (r["date"], not r["cee"]), reverse=False)
    resultats.sort(key=lambda r: not r["cee"])

    for r in resultats:
        badge = "🔋 CEE" if r["cee"] else "🌱 Environnement"
        label = "Résumé IA" if has_anthropic_key else "Contenu brut (pas de résumé IA)"
        with st.container(border=True):
            st.markdown(f"**{badge}** · *{r['nature']}* · {r['date']} · _{label}_")
            st.subheader(r["titre"])
            st.markdown(r["resume"])

    # --- Export ---
    st.divider()
    range_label = f"{start_date.isoformat()}_au_{end_date.isoformat()}" if start_date != end_date else start_date.isoformat()
    col1, col2 = st.columns(2)
    with col1:
        st.download_button(
            "📥 Télécharger en JSON",
            data=json.dumps(resultats, ensure_ascii=False, indent=2),
            file_name=f"veille_jo_environnement_{range_label}.json",
            mime="application/json",
            use_container_width=True,
        )
    with col2:
        periode_str = f"du {start_date.isoformat()} au {end_date.isoformat()}" if start_date != end_date else start_date.isoformat()
        texte_export = f"VEILLE JO — ENVIRONNEMENT & CEE — {periode_str}\n" + "=" * 50 + "\n\n"
        for r in resultats:
            badge = "[CEE]" if r["cee"] else "[Environnement]"
            texte_export += f"{badge} {r['titre']} ({r['nature']}) — {r['date']}\n\n{r['resume']}\n\n{'-'*50}\n\n"
        st.download_button(
            "📥 Télécharger en TXT",
            data=texte_export,
            file_name=f"veille_jo_environnement_{range_label}.txt",
            mime="text/plain",
            use_container_width=True,
        )


if __name__ == "__main__":
    main()
