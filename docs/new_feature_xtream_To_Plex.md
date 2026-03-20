Spécification fonctionnelle
L’application doit convertir un catalogue Xtream/VOD en bibliothèque “Plex-like” locale, sans stocker les médias eux-mêmes, en générant un fichier .strm par film ou épisode.

Plex est plus fiable quand les contenus sont organisés avec un nommage clair en dossiers distincts pour les films et les séries, avec titres et années pour aider l’identification.
​
​
Les métadonnées locales via .nfo peuvent être ajoutées en bonus, mais Plex ne les supporte pas de manière native et cela peut dépendre d’outils tiers ou de la configuration.

Périmètre
Entrée : catalogue VOD distant issu de Xtream.

Sortie : arborescence disque compatible Plex.

Médias : aucun fichier vidéo stocké localement, uniquement des .strm et métadonnées associées.

Cibles : films et séries, pas live TV.

Arborescence cible
text
Media/
  Films/
    Dune (2021)/
      Dune (2021).strm
      movie.nfo
      poster.jpg
  Series/
    The Last of Us/
      Season 01/
        The Last of Us S01E01.strm
        The Last of Us S01E02.strm
      tvshow.nfo
      poster.jpg
Cette structure suit la logique de rangement que Plex comprend le mieux pour les bibliothèques locales.
​
​

Format des fichiers
Un fichier .strm doit contenir une seule URL de lecture finale, en texte brut, pointant vers le flux VOD.

Si tu génères des .nfo, ils doivent servir d’aide au matching et au contexte, mais il faut prévoir que Plex peut les ignorer selon l’agent ou la configuration.

Comportement attendu
Générer/mettre à jour l’arborescence à chaque synchronisation.

Éviter les doublons.

Gérer renommage, suppression, désactivation d’un titre.

Prévoir un mode “scan incrémental” plutôt qu’un rebuild complet.

Ajouter une couche d’URL stable si le lien Xtream expire ou change.

Contraintes techniques
Plex n’est pas conçu pour indexer directement un simple lien HTTP comme un film de bibliothèque classique, donc la stratégie .strm est le meilleur compromis pour simuler des fichiers sans stocker la vidéo.

Les symlinks sont généralement moins adaptés à ton cas, car ils redirigent vers un chemin disque et ne portent pas l’URL du flux.

Pour les séries, le nommage des épisodes doit être strict et prévisible afin de maximiser le matching et éviter les confusions.
​
