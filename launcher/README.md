# Lanceur Doli Local Edit

Ce répertoire contient le composant local multiplateforme. Il n'embarque aucun
éditeur : le poste utilise son association de fichiers par défaut, un éditeur choisi localement
ou une commande configurée localement. Le serveur ne peut fournir ni programme ni argument de
commande.

## Choix technique

Le lanceur utilise uniquement la bibliothèque standard Python. Les sources restent testables
avec Python 3.10 ou plus récent ; les exécutables publiables sont construits exclusivement avec
Python 3.14.7 et PyInstaller 6.22.2 dans les environnements isolés et verrouillés décrits dans
[le README du dépôt](../README.md). La signature reste une porte de livraison : un exécutable non signé est destiné
au développement, pas à un déploiement utilisateur.

## Garanties du lanceur

- seules les origines Dolibarr explicitement approuvées dans la configuration locale sont
  acceptées ; HTTP est limité à la boucle locale de développement ;
- le ticket de 120 secondes est échangé immédiatement par le processus de protocole ; le
  processus de travail est ensuite lancé sans secret dans sa ligne de commande ;
- le jeton Bearer passe au processus de travail par un tube anonyme, reste uniquement en mémoire
  et n'est jamais écrit dans le manifeste de reprise ;
- les redirects HTTP sont refusés afin de ne jamais transférer le Bearer vers une autre URL ;
- le téléchargement est limité à 1 Gio puis contrôlé par sa taille finale et son SHA-256 ; les
  en-têtes `Content-Length` et `ETag`, facultatifs avec une réponse HTTP `chunked`, sont aussi
  vérifiés lorsqu'ils sont présents ; le fichier est ensuite stocké avec des permissions privées ;
- une révision stable est copiée dans un instantané avant dépôt. `If-Match`, l'empreinte de base
  et la version de location sont envoyés au serveur ;
- la copie locale est conservée en cas de coupure, conflit ou confirmation incomplète.
- une origine inconnue n'est ajoutée qu'après confirmation locale explicite au premier clic ;
- sous Windows et Linux, l'exécutable autonome s'installe dans le profil courant et enregistre le
  protocole sans élévation (`HKCU` ou entrée XDG).

## Développement

Depuis la racine du dépôt :

```bash
PYTHONPATH=launcher/src python3 -m unittest discover -s launcher/tests -v
PYTHONPATH=launcher/src python3 -m dolilocaledit_launcher config-path
```

Le parcours HTTP de bout en bout avec Dolibarr reste qualifié dans le dépôt privé du module ; ce
dépôt public exerce uniquement la frontière locale et le paquetage du lanceur.

Pour une installation Python locale de développement :

```bash
python3 -m pip install --user -e ./launcher
dolilocaledit-launcher trust https://erp.example
dolilocaledit-launcher register --executable /chemin/absolu/vers/dolilocaledit-launcher
```

`register` gère l'inscription par utilisateur du protocole sous Windows et Linux. Sous Linux,
`install` copie le binaire sous `~/.local/bin` et `uninstall` retire le programme et son entrée
XDG tout en conservant les reprises. Sous macOS,
l'inscription devra être fournie dans le paquet `.app` signé.

Le mode éditable exige un `pip` et un `setuptools` récents. Si la distribution fournit une version
ancienne, les mettre à niveau dans le profil utilisateur avant l'installation :

```bash
python3 -m pip install --user --upgrade 'pip>=24' 'setuptools>=68' wheel
```

## Configuration locale

Le chemin exact est donné par `dolilocaledit-launcher config-path`. Sous POSIX, le fichier doit
appartenir à l'utilisateur et avoir les permissions `0600`. Exemple :

```json
{
  "trusted_origins": [
    "https://erp.example"
  ],
  "editors": {
    ".docx": [
      "/usr/bin/libreoffice",
      "--writer",
      "{file}"
    ]
  },
  "untracked_editor_extensions": [],
  "system_default_editor_extensions": [],
  "poll_seconds": 1.0,
  "stable_seconds": 2.0,
  "api_timeout_seconds": 30.0
}
```

Chaque éditeur configuré doit être un chemin absolu. Les arguments sont un tableau et sont
passés directement au processus avec `shell=False`. `{file}` est remplacé par le chemin local ;
s'il est absent, ce chemin est ajouté comme dernier argument. La sortie d'un processus est
utilisée comme fermeture uniquement pour les éditeurs de terminal reconnus exécutés au premier
plan, sans mode distant ou détaché. Les autres applications utilisent le suivi du document ou
la confirmation locale **Terminer l'édition**.

Sous Windows ou Linux graphique, si aucun choix n'est déjà enregistré, le lanceur affiche un sélecteur local pour
chaque format pris en charge. Il propose les applications reconnues, l'association Windows
ou Linux actuelle et **Choisir une autre application…**, qui ouvre un parcours limité aux exécutables du
poste. Linux utilise Zenity ou KDialog. La case **Toujours utiliser ce choix** est décochée par défaut. Lorsqu'elle est cochée, la
commande ou le choix de l'association système est enregistré uniquement dans ce fichier de
configuration. Pour rétablir la question, exécuter
`dolilocaledit-launcher forget-editor .docx`. Les applications reconnues sont trouvées via les
entrées Windows « App Paths » et les emplacements d'installation usuels ; aucune liste de
programmes ne vient de Dolibarr.

Office et LibreOffice peuvent réutiliser un processus déjà ouvert. Le lanceur suit donc le
document précis lorsque le profil local le permet : document Office dans son instance Windows,
verrou propriétaire LibreOffice observé, ou processus de terminal reconnu au premier plan. Une
erreur de détection ne vaut jamais fermeture. Les sauvegardes intermédiaires conservent la
location ; la dernière version stable est publiée après fermeture effective du document.
La fermeture de la dernière fenêtre Office reste observable lorsque le processus propriétaire
déjà identifié se termine ; une erreur COM avec un processus encore vivant ne vaut pas fermeture.

Une session dont la durée maximale est atteinte sans changement enregistré sur disque est
annulée sans boîte d'erreur. Le lanceur conserve la copie et le manifeste `expired_unchanged`
si l'éditeur est encore ouvert ou si sa fermeture n'est pas observable, sur tous les systèmes.
Cette reprise doit être vérifiée avant suppression, car l'éditeur peut encore enregistrer le
fichier après l'échéance. La même conservation s'applique à une interruption réseau même lorsque
le document n'a pas encore changé sur disque. Un éditeur qui remplace son fichier lors de
l'enregistrement dispose d'un délai de 30 secondes pour recréer le document ; les heartbeats
restent actifs pendant ce délai.

Sans association explicite, le lanceur ouvre l'application système et tente le même suivi du
document. Lorsque la fermeture n'est pas observable, le dialogue local **Terminer l'édition**
permet de confirmer l'enregistrement et la fermeture. La copie de travail reste conservée avec
l'état `published_recovery` après publication. Les dossiers conservés sont listés par :

```bash
dolilocaledit-launcher recoveries
```

Cette conservation prudente évite de perdre une sauvegarde lorsque la fermeture de l'application
ne peut pas être observée. Il ne faut pas supprimer une reprise tant que son contenu n'a pas été
vérifié.

## Construction d'un exécutable

PyInstaller doit construire chaque cible sur son propre système ; ce n'est pas un compilateur
croisé. La version de l'outil est figée dans `requirements-build.txt` et vérifiée par `build.py` :

```bash
python3 -m pip install -r launcher/requirements-build.txt
python3 launcher/build.py --output-directory dist/launcher
```

Une construction exécutée sous Windows produit aussi `install.cmd`. Distribuer les deux fichiers
dans le même dossier ; l'utilisateur lance `install.cmd` une fois. L'exécutable est copié sous
`%LOCALAPPDATA%\Programs\DoliLocalEdit` avec un nom immuable comprenant sa version et son SHA-256.
Le protocole HKCU pointe ensuite sur ce nouveau fichier. Une mise à niveau n'arrête aucune
ancienne instance : les fichiers encore utilisés sont conservés et signalés dans la confirmation,
puis nettoyés automatiquement lors d'un lancement ultérieur. Au premier clic dans Dolibarr,
une boîte locale affiche seulement l'origine canonique à approuver, jamais le ticket.
Le binaire Windows utilise le sous-système graphique : il n'ouvre pas de console pendant
l'édition. En cas d'échec utile à l'utilisateur, sa boîte indique le document lorsqu'il a pu être
identifié, l'origine Dolibarr, la présence de changements enregistrés, l'éventuel dossier de
reprise, l'état de libération du verrou, une action conseillée et un code de diagnostic stable.
Un échec antérieur à l'échange indique explicitement que le document n'est pas encore déterminé.
Le worker, qui doit survivre au court processus du gestionnaire de protocole, est lancé comme une
instance PyInstaller indépendante afin que leurs répertoires `_MEI` puissent être supprimés sans
course ni avertissement.
Le fichier de version Windows embarque le nom de société **Experts Conseils Chanton**, le nom de
produit **Doli Local Edit**, la description, le copyright et les versions fichier/produit lues
depuis `pyproject.toml`. Vérifier ces propriétés avant d'appliquer la signature Authenticode.
Pour un déploiement géré, `dolilocaledit-launcher.exe install --quiet` effectue la même
installation par utilisateur sans boîte de confirmation.

L'installation interactive affiche une boîte de réussite avec la version, le chemin installé et
la confirmation d'enregistrement du protocole, ou une boîte d'échec avec un code exploitable.
Le bouton **Tester le lanceur** de Dolibarr prépare ensuite une opération
`dolilocaledit://check`, puis demande un second clic explicite pour l'ouvrir. Le lanceur accepte
les formes `check?…` et `check/?…` normalisées par les navigateurs, valide l'origine approuvée et
consomme le ticket éphémère pour annoncer sa version et sa plateforme, sans télécharger ni ouvrir
de document.

Une construction Linux produit le binaire et `install.sh`. Le script de livraison
`scripts/package-launchers.py` les place dans une archive `tar.gz` qui préserve leurs bits
d'exécution. L'utilisateur décompresse puis lance `install.sh`; aucune installation de Python
n'est requise. Réexécuter l'installateur remplace atomiquement la version Linux précédente tout
en laissant les sessions déjà ouvertes utiliser leur ancien exécutable. Pour une livraison,
`scripts/sign-linux-release.sh` produit une signature OpenPGP
détachée de l'archive et de `SHA256SUMS`; la publication contient aussi la clé publique, jamais la
clé privée.

Les exécutables et leur code source correspondant sont publiés séparément dans le dépôt public
[`GregChant/dolilocaledit-launcher`](https://github.com/GregChant/dolilocaledit-launcher). Le
lanceur Windows 1.0.5 est signé et horodaté par l'éditeur ; le binaire Linux x86-64 est construit
et lancé dans la porte locale. L'archive et `SHA256SUMS` sont signés par la clé OpenPGP de
livraison `A51F BBAB 9A50 9277 1768 E839 8C95 07E9 997C 0569`. macOS doit encore être construit,
testé, signé et notarié. Voir la
[présentation de la publication](../README.md).
