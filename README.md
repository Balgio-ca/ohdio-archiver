# ohdio-archiver

Outil auto-hébergé qui conserve une copie personnelle des livres audio diffusés gratuitement sur OHdio (Radio-Canada). Il classe chaque livre par catégorie, le renomme proprement, intègre les métadonnées et vérifie chaque fichier avant de le considérer comme archivé. Une interface web permet de choisir les catégories et de suivre l'archivage, et le résultat est prêt pour [Audiobookshelf](https://www.audiobookshelf.org/).

> **Avis important : usage personnel seulement.** Lire la section [Avis légal](#avis-légal) avant toute utilisation.

## Avis légal

**Ce projet n'est pas un outil de piratage.** Il est conçu uniquement pour conserver, à des fins personnelles et privées, une copie d'œuvres que Radio-Canada rend déjà accessibles gratuitement au public, par exemple pour les écouter hors ligne ou sur son propre serveur multimédia.

- **Aucune affiliation.** Ce projet n'est ni affilié, ni approuvé, ni soutenu par la Société Radio-Canada / CBC, par OHdio ou par yodio.ca. Les noms et marques cités appartiennent à leurs propriétaires respectifs.
- **Droits d'auteur.** Les livres audio restent la propriété de leurs auteurs, éditeurs, interprètes et de Radio-Canada. Ce projet ne transfère aucun droit sur les œuvres.
- **Interdit de redistribuer.** Il est interdit de partager, revendre, téléverser, diffuser publiquement ou mettre en ligne les fichiers obtenus, y compris sur des sites de torrent, des serveurs multimédias partagés ou des services infonuagiques publics.
- **Aucun contournement.** L'outil récupère uniquement les fichiers publics que le lecteur officiel reçoit déjà. Il ne contourne aucun verrou numérique (DRM), aucune authentification et aucun abonnement. S'il fallait un jour contourner une protection, ce projet ne le ferait pas.
- **Ta responsabilité.** Chaque utilisateur doit vérifier que son usage respecte les lois de son pays (au Canada, la *Loi sur le droit d'auteur*), ainsi que les conditions d'utilisation de Radio-Canada et des services utilisés. En cas de doute, n'utilise pas cet outil.
- **Aucune garantie, aucune responsabilité.** Le logiciel est fourni « tel quel », sans garantie d'aucune sorte (voir [LICENSE](LICENSE)). Les auteurs et contributeurs ne sont pas responsables de l'usage qui en est fait, ni des dommages, pertes de données ou conséquences juridiques qui pourraient en découler.
- **Retrait.** Si tu es titulaire de droits et que ce projet te pose problème, ouvre une *issue* : elle sera traitée rapidement.

Pour soutenir les créateurs, écoute aussi les œuvres sur [OHdio](https://ici.radio-canada.ca/ohdio) et achète les livres qui te plaisent.

*English summary: this is a personal archiving tool for audiobooks that Radio-Canada already streams for free. It is not affiliated with Radio-Canada/CBC, OHdio or yodio.ca, does not bypass any DRM, and must not be used to redistribute content. You are solely responsible for complying with copyright law and the terms of service that apply to you. Provided "as is", without warranty or liability.*

## Fonctionnement

1. Le catalogue vient de l'API publique de [yodio.ca](https://yodio.ca/shows/audiobooks), un client alternatif d'OHdio.
2. Seuls les livres des catégories choisies sont retenus : Roman, Essai, Biographie, Poésie, et les catégories Jeunesse 0-5, 6-8, 9-12 et 13-17 ans.
3. Chaque piste est téléchargée, puis sa taille et sa durée sont vérifiées.
4. L'audio est réemballé en `.m4a` sans réencodage, avec titre, auteur, numéro de piste, genre, année et pochette.
5. Chaque fichier est relu, puis son empreinte SHA-256 est enregistrée. Un livre n'est marqué complet que si toutes ses pistes passent.
6. Toutes les N heures, le catalogue est relu et les nouveaux livres des catégories choisies sont ajoutés automatiquement.

L'outil fait une pause entre les requêtes et télécharge un fichier à la fois, pour ne pas surcharger les serveurs.

## Installation avec Docker (NAS)

```bash
git clone https://github.com/Balgio-ca/ohdio-archiver.git
cd ohdio-archiver
cp .env.example .env      # adapter PUID, PGID et OHDIO_ARCHIVE_PATH
docker compose up -d --build
```

L'interface est ensuite accessible sur `http://<ip-de-la-machine>:8765`. Elle permet de :

- cocher les catégories à télécharger automatiquement ;
- régler la fréquence de vérification des nouveautés ;
- chercher dans le catalogue et voir l'état de chaque livre ;
- forcer ou exclure un livre précis, peu importe sa catégorie ;
- lancer ou arrêter un archivage et suivre la progression en direct.

**L'interface n'a pas d'authentification.** Garde-la sur ton réseau local et ne l'expose pas sur Internet.

Dans Audiobookshelf, crée une bibliothèque qui pointe vers `<OHDIO_ARCHIVE_PATH>/Livres audio`. Chaque dossier contient un `metadata.json` lu par Audiobookshelf, qui fournit les vrais auteurs, le genre, l'éditeur et l'année.

## Ligne de commande

Prérequis : Python 3.11+ et `ffmpeg`. Aucune librairie Python externe.

```bash
python3 ohdio.py catalog                     # décompte par catégorie
python3 ohdio.py list                        # livres retenus par les filtres
python3 ohdio.py download --dry-run          # simulation
python3 ohdio.py download                    # archivage (relancer pour reprendre)
python3 ohdio.py download --include roman essai --limit 5
python3 ohdio.py download --ids 105711       # un livre précis
python3 ohdio.py verify --deep               # SHA-256 + relecture de toute l'archive
python3 ohdio.py serve                       # interface web + archivage automatique
```

Dans Docker, les mêmes commandes passent par `docker compose run --rm ohdio <commande>`.

Les réglages sont dans `config.toml` (ou `config/config.toml` avec Docker). L'alias `jeunesse` couvre toutes les catégories d'âge.

## Structure de l'archive

```
Livres audio/
  Roman/
    Auteur - Titre/
      01 - Partie 1.m4a
      02 - Partie 2.m4a
      cover.jpg
      description.txt
      metadata.json     fiche Audiobookshelf
      manifest.json     durées, tailles, SHA-256, source
  Jeunesse 0-5 ans/
    ...
catalogue.json          cache du catalogue
index.csv               un livre par ligne, avec son statut
```

## Licence

[MIT](LICENSE). La licence couvre uniquement le code de ce projet, pas les œuvres qu'il permet d'archiver.
