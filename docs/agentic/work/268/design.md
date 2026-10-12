# Design — #268

DESIGN: PASS. Le contrat mobile v2 prime. GET patients : id, firstname, lastname, city, phone, birthday exactement ; enveloppe count/page/page_size/next_page/previous_page/results. Pagination défaut 20, maximum 100, page lexical [1-9][0-9]* jusqu'à 2147483647, tri id ASC. Query unique page/page_size/search seulement, détail futur sans query. Search ≤100 points de code avant NFD/retrait M/casefold/split Unicode ; AND termes OR champs. Littéraux %, _, * comparés littéralement par INSTR/STRPOS.

Auth puis droits puis query. Sans Nurse liste vide, aucun get_or_create ; staff ne contourne rien. HEAD mêmes statuts/en-têtes sans corps ; OPTIONS authentifié sans données métier, annonce GET/HEAD/OPTIONS. Mutations 405 après auth. Erreurs exactes code/message français normatif et WWW-Authenticate Bearer sur 401. Cache-Control private,no-store et Pragma no-cache, pas ETag/304 ; renderer JSON même si Accept HTML. 429 Retry-After entier si configuré, 503 si panne temporaire maîtrisée. v1 et M02 inchangés.

Vie privée : aucune query/identifiant/jeton/corps dans journaux de lecture applicatifs ; scope middleware et filtre logs, traces Sentry de ces lectures supprimées ; configuration Gunicorn dédiée. Qualification proxy/hébergement réelle reste un gate avant données réelles. UI/Android hors périmètre, second 401 prouve serveur seulement.

Gate PASS : succès/vide/fin/query/auth/droits/indisponibilité et rollback représentés ; architecture existante conservée.
