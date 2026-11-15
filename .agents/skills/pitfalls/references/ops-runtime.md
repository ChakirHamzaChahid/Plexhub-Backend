# Pièges connus — Runtime, logs Windows, I/O bloquant et dépendances

- **[#9]** **Rotation de logs Windows** : `SafeRotatingFileHandler` avale les `PermissionError` (`main.py:30-35`) quand un autre process tient le fichier. Ne pas supprimer ce garde-fou.

- **[#11]** **Appels bloquants** (`sqlite3.backup`, init/inférence ONNX `embedding_service.py:124,130`) → `asyncio.to_thread` ; ne jamais bloquer la boucle d'événements. Les téléchargements d'images Plex passent par un `ThreadPoolExecutor` dédié (8 threads, `storage.py:36`).
