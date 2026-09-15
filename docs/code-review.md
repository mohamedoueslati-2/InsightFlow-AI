# Analyse du projet et améliorations

## Architecture observée

Le frontend HTML/CSS/JavaScript appelle FastAPI. `backend/main.py` lit et écrit les fichiers locaux et orchestre le profilage, le nettoyage puis les conversations par problème. Pandas calcule les métriques ; Gemini choisit des outils fermés ; Pydantic structure les rapports. Le nettoyage travaille sur une copie avec validation et annulation locales à chaque opération.

La séparation entre raisonnement et exécution est utile : les métriques et actions exécutées constituent des preuves vérifiables. Les principaux écarts se situent entre une décision du modèle, la mesure réellement obtenue et le statut présenté à l’utilisateur.

## Corrections réalisées

| Point observé | Changement | Raison |
| --- | --- | --- |
| Le fallback utilisait tout message comme valeur de remplacement et cherchait un fragment « supprim » pour supprimer des lignes. | Analyse conservatrice des commandes explicites ; les questions et négations restent sans effet. | « Ne supprime pas » ne doit jamais être interprété comme une suppression. |
| Le chat exposait tous les outils de mutation, sans restriction de colonne en code. | Inspection et deux outils manuels contrôlés par colonne, type de problème et valeur autorisée. | Une conversation sur une colonne ne doit pas modifier silencieusement une autre colonne. |
| Toute action faisait disparaître le problème courant. | Vérification des valeurs manquantes dans le résultat ; conservation des autres problèmes. | Une action appliquée n’est pas une preuve de résolution. |
| Le modèle pouvait omettre des problèmes du profilage dans sa réponse finale ; un flag était considéré comme une correction. | Réintégration des constats sans correction enregistrée ; les flags ne résolvent plus les constats. | Éviter les rapports artificiellement rassurants. Cette vérification reste partielle pour les catégories autres que les valeurs manquantes. |
| Le journal et les mesures n’étaient pas actualisés après une conversation. | Ajout des étapes et des mesures du dernier échange. | Permettre de retracer ce qui a réellement changé. |
| Le chat était masqué dans les détails techniques ; les conversations résolues n’étaient plus rendues. | Chat visible et historique des conversations terminées. | L’utilisateur peut lire la dernière réponse et retrouver sa décision. |
| Envois multiples possibles depuis le rapport. | Verrou pendant la requête, envoi par Entrée, reprise de la saisie après erreur. | Réduire les demandes accidentelles en double dans cette interface. Ce verrou ne protège pas plusieurs navigateurs. |

## Évolution du chat : propositions exécutables

Le parcours proposition → aperçu → application est maintenant implémenté avec 12 opérations, génération d’e-mails fictifs séquentiels et marquage des cellules synthétiques. L’aperçu mémorise les paramètres et une empreinte du dataset. Une confirmation applique ces paramètres ; un aperçu périmé est refusé. Des cartes affichent impact et exemples, avec choix par bouton ou réponse courte lorsque le choix est univoque. Les commandes directes historiques restent compatibles.

Les mutations de nettoyage/chat sont sérialisées dans le processus FastAPI. Cela ne remplace pas une transaction multi-fichiers ni un verrou entre plusieurs workers. Les points ci-dessous décrivent les limites initiales et les suites restant utiles.

## Suite recommandée, par priorité

### 1. Fiabiliser les écritures et la concurrence

`backend/main.py` lit, modifie puis écrit le CSV, le pickle et le rapport séparément. Les écritures sont maintenant sérialisées dans un même processus. Avec plusieurs workers, deux conversations simultanées ou un re-nettoyage peuvent encore travailler sur le même ancien état et écraser le résultat de l’autre. Introduire un identifiant/version de dataset, une transaction de sauvegarde et une vérification de version sur chaque mutation. Un verrou Python seul ne suffira pas avec plusieurs processus.

La gestion des clés dans les agents modifie `os.environ`, partagé entre les requêtes. Remplacer cette sélection globale par un client configuré pour chaque tentative. Ajouter un test de deux requêtes concurrentes avec des clés simulées différentes.

### 2. Compléter le parcours proposition → aperçu → application

Le parcours est implémenté avec paramètres enregistrés, empreinte du dataset, aperçu et confirmation. Les prochains ajouts utiles sont l’annulation persistante, les propositions de plusieurs opérations ordonnées, et des états d’erreur plus détaillés. Le modèle ne doit pas réinterpréter une proposition déjà confirmée.

### 3. Améliorer les décisions de nettoyage

Dans `tools.py`, une date future n’est pas nécessairement une erreur : elle peut représenter une échéance. La correction du décalage d’année se base actuellement sur la distribution des dates, sans règle métier explicite. Demander le rôle de la colonne ou une règle de validité avant de modifier ces cas.

De même, un outlier statistique n’est pas nécessairement une valeur invalide, et une normalisation de casse peut altérer un identifiant sensible à la casse. Documenter des contraintes par colonne et utiliser une proposition plutôt qu’une correction automatique quand le sens métier est ambigu.

Après chaque changement important, recontrôler les dépendances entre colonnes et les problèmes existants. La validation actuelle vérifie surtout la métrique ciblée ; elle ne garantit pas à elle seule l’absence de tout nouveau problème métier.

### 4. Rendre les scores comparables

Le rapport calcule maintenant le score après nettoyage sur les colonnes métier enregistrées, en excluant les colonnes d’audit. Il reste utile de distinguer complétude, validité métier et qualité statistique : une imputation ou des identifiants fictifs peuvent améliorer la complétude sans restituer les données réelles.

`rows_affected` additionne les lignes touchées par chaque action : une même ligne peut être comptée plusieurs fois, alors que le schéma annonce des lignes distinctes. Conserver des identifiants de ligne stables ou présenter explicitement un nombre cumulé d’interventions.

### 5. Préparer des fichiers plus volumineux

L’upload et la lecture renvoient toutes les lignes au navigateur. Ajouter pagination et aperçu limité, limite de taille d’upload et traitement asynchrone des longues analyses. Les noms de fichiers et chemins doivent être validés et séparés d’un identifiant interne unique pour éviter collisions et chemins imprévus.

### 6. Mesurer la qualité du raisonnement

Créer un jeu d’évaluation de conversations : question, négation, valeur ambiguë, demande sur une autre colonne, donnée métier légitime mais atypique, erreur fournisseur et changement concurrent. Mesurer surtout les modifications non demandées, les fausses résolutions et les décisions correctes de ne pas agir. Versionner les prompts et les dépendances pour comparer les résultats entre modèles.

## Vérification et limites

Les tests de profilage et de nettoyage existants ont été exécutés sans clés actives, avec les simulations de rotation prévues par ces tests. Une nouvelle suite couvre les commandes du chat, les restrictions des outils, la conservation des problèmes et les flags. La syntaxe JavaScript est vérifiée.

Les appels réels à Gemini n’ont pas été testés. Le parcours e-mails fictifs a été vérifié dans un navigateur, sur des données temporaires : demande, aperçu, application, score et historique. Les 26 tests de chat/propositions et les suites existantes sont complétés par ce contrôle. Le chat ne propose pas encore de streaming, d’annulation persistante ni de transaction multi-fichiers. Le catalogue contient 12 opérations ; une règle métier en dehors de ce catalogue nécessite un outil supplémentaire.
