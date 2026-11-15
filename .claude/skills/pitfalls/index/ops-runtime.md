# Index des pièges — ops-runtime

> Généré par `python .claude/tools/pitfalls-index.py` depuis `.claude/skills/pitfalls/references/ops-runtime.md` : un titre par piège (son segment en gras). Ne pas éditer ici — ajouter le piège dans `references/`, puis relancer le script.

## ops-runtime — Runtime, logs Windows, I/O bloquant et dépendances (2)

Texte complet : `.claude/skills/pitfalls/references/ops-runtime.md`

- [#9] : **Rotation de logs Windows** : `SafeRotatingFileHandler` avale les `PermissionError` (`main.py:30-35`) quand un autre process tient le fichier
- [#11] : **Appels bloquants** (`sqlite3.backup`, init/inférence ONNX `embedding_service.py:124,130`) → `asyncio.to_thread`
