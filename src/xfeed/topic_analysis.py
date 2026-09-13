import hashlib
import json
import math
import re
import unicodedata
import warnings
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Self

from sklearn.decomposition import NMF  # type: ignore[import-untyped]
from sklearn.exceptions import ConvergenceWarning  # type: ignore[import-untyped]
from sklearn.feature_extraction.text import TfidfVectorizer  # type: ignore[import-untyped]


TOKEN_RE = re.compile(r"(?u)\b[^\W\d_]\w+\b")
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
HANDLE_RE = re.compile(r"(?<!\w)@[A-Za-z0-9_]{1,15}\b")

# This list is part of the en-tr-1 analysis contract. Extend it only by publishing a
# new stop-word version so an older corpus/configuration remains reproducible.
STOP_WORDS_EN_TR_V1: frozenset[str] = frozenset(
    {
        # English
        "a",
        "about",
        "after",
        "again",
        "against",
        "all",
        "also",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "because",
        "been",
        "before",
        "being",
        "between",
        "both",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "doing",
        "down",
        "during",
        "each",
        "few",
        "for",
        "from",
        "further",
        "had",
        "has",
        "have",
        "having",
        "he",
        "her",
        "here",
        "hers",
        "herself",
        "him",
        "himself",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "itself",
        "just",
        "me",
        "more",
        "most",
        "my",
        "myself",
        "no",
        "nor",
        "not",
        "now",
        "of",
        "off",
        "on",
        "once",
        "only",
        "or",
        "other",
        "our",
        "ours",
        "ourselves",
        "out",
        "over",
        "own",
        "same",
        "she",
        "should",
        "so",
        "some",
        "such",
        "than",
        "that",
        "the",
        "their",
        "theirs",
        "them",
        "themselves",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "to",
        "too",
        "under",
        "until",
        "up",
        "very",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
        "yours",
        "yourself",
        "yourselves",
        # Turkish
        "acaba",
        "ama",
        "ancak",
        "artık",
        "aslında",
        "az",
        "bana",
        "bazen",
        "bazı",
        "belki",
        "ben",
        "benden",
        "beni",
        "benim",
        "beri",
        "beş",
        "bile",
        "bir",
        "biraz",
        "birçok",
        "biri",
        "birkaç",
        "biz",
        "bizden",
        "bize",
        "bizi",
        "bizim",
        "bu",
        "buna",
        "bunda",
        "bundan",
        "bunu",
        "bunun",
        "burada",
        "böyle",
        "da",
        "daha",
        "dahi",
        "de",
        "defa",
        "değil",
        "diye",
        "diğer",
        "dokuz",
        "dolayı",
        "dört",
        "edecek",
        "eden",
        "ederek",
        "edilecek",
        "ediliyor",
        "edilmesi",
        "ediyor",
        "eğer",
        "elbette",
        "en",
        "fakat",
        "gibi",
        "göre",
        "hala",
        "hangi",
        "hatta",
        "hem",
        "hep",
        "hepsi",
        "her",
        "hiç",
        "için",
        "ile",
        "ise",
        "kez",
        "ki",
        "kim",
        "kimden",
        "kime",
        "kimi",
        "mı",
        "mi",
        "mu",
        "mü",
        "nasıl",
        "ne",
        "neden",
        "nerde",
        "nerede",
        "nereye",
        "niye",
        "niçin",
        "o",
        "olan",
        "olarak",
        "oldu",
        "olduğu",
        "olmak",
        "olması",
        "olmayan",
        "olmaz",
        "olsa",
        "olsun",
        "on",
        "ona",
        "ondan",
        "onlar",
        "onlardan",
        "onları",
        "onların",
        "onu",
        "onun",
        "oysa",
        "pek",
        "rağmen",
        "sana",
        "sanki",
        "sekiz",
        "sen",
        "senden",
        "seni",
        "senin",
        "siz",
        "sizden",
        "size",
        "sizi",
        "sizin",
        "sonra",
        "şey",
        "şu",
        "şuna",
        "şunda",
        "şundan",
        "şunu",
        "tarafından",
        "ve",
        "veya",
        "ya",
        "yani",
        "yedi",
        "yerine",
        "yine",
        "yoksa",
        "zaten",
    }
)


class TopicModelState(StrEnum):
    BUILT = "built"
    INSUFFICIENT = "insufficient"


class TopicAnalysisState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    INSUFFICIENT = "insufficient"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class AnalysisPost:
    id: int
    text: str
    updated_at: str


@dataclass(frozen=True)
class TopicAnalysisConfig:
    method: str = "tfidf_nmf"
    method_version: str = "1"
    max_features: int = 5_000
    max_df: float = 0.90
    max_iter: int = 400
    random_state: int = 42
    secondary_threshold: float = 0.35
    stop_words_version: str = "en-tr-1"


@dataclass(frozen=True)
class TopicAnalysisMetadata:
    method: str
    method_version: str
    config_json: str
    corpus_fingerprint: str

    def with_fingerprint(self, corpus_fingerprint: str) -> Self:
        return type(self)(
            method=self.method,
            method_version=self.method_version,
            config_json=self.config_json,
            corpus_fingerprint=corpus_fingerprint,
        )


@dataclass(frozen=True)
class DetectedTopicDraft:
    cluster_index: int
    display_label: str
    keywords_json: str


@dataclass(frozen=True)
class TopicAssignmentDraft:
    post_id: int
    cluster_index: int
    rank: int
    normalized_score: float


@dataclass(frozen=True)
class TopicModelDraft:
    state: TopicModelState
    config_json: str
    corpus_fingerprint: str
    topics: tuple[DetectedTopicDraft, ...]
    assignments: tuple[TopicAssignmentDraft, ...]

    @property
    def metadata(self) -> TopicAnalysisMetadata:
        config = json.loads(self.config_json)
        return TopicAnalysisMetadata(
            method=str(config["method"]),
            method_version=str(config["method_version"]),
            config_json=self.config_json,
            corpus_fingerprint=self.corpus_fingerprint,
        )


@dataclass(frozen=True)
class TopicAnalysisRun:
    id: int
    method: str
    method_version: str
    config_json: str
    corpus_fingerprint: str
    state: TopicAnalysisState
    diagnostic: str | None
    created_at: str
    completed_at: str | None
    is_active: bool


def normalize_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).casefold()
    value = URL_RE.sub(" ", value)
    value = HANDLE_RE.sub(" ", value)
    return value.replace("#", " ")


def tokenize(text: str) -> list[str]:
    return [
        token
        for token in TOKEN_RE.findall(normalize_text(text))
        if token not in STOP_WORDS_EN_TR_V1
    ]


def make_corpus_fingerprint(posts: tuple[AnalysisPost, ...], config_json: str) -> str:
    digest = hashlib.sha256(config_json.encode("utf-8"))
    for post in sorted(posts, key=lambda item: item.id):
        digest.update(f"\n{post.id}\0{post.updated_at}\0{post.text}".encode("utf-8"))
    return digest.hexdigest()


class TopicAnalyzer:
    def __init__(self, config: TopicAnalysisConfig | None = None) -> None:
        self.config = config or TopicAnalysisConfig()

    @property
    def config_json(self) -> str:
        return json.dumps(asdict(self.config), sort_keys=True, separators=(",", ":"))

    def metadata(self, corpus_fingerprint: str) -> TopicAnalysisMetadata:
        return TopicAnalysisMetadata(
            method=self.config.method,
            method_version=self.config.method_version,
            config_json=self.config_json,
            corpus_fingerprint=corpus_fingerprint,
        )

    def analyze(self, posts: tuple[AnalysisPost, ...]) -> TopicModelDraft:
        ordered = tuple(sorted(posts, key=lambda item: item.id))
        candidates = tuple(post for post in ordered if post.text.strip())
        fingerprint = make_corpus_fingerprint(candidates, self.config_json)
        usable = tuple(post for post in candidates if tokenize(post.text))
        if len(usable) < 10:
            return self._insufficient(fingerprint)

        min_df = 1 if len(usable) <= 19 else 2
        vectorizer = TfidfVectorizer(
            preprocessor=normalize_text,
            tokenizer=tokenize,
            token_pattern=None,
            ngram_range=(1, 2),
            sublinear_tf=True,
            norm="l2",
            max_features=self.config.max_features,
            min_df=min_df,
            max_df=self.config.max_df,
        )
        try:
            matrix = vectorizer.fit_transform(post.text for post in usable)
        except ValueError:
            return self._insufficient(fingerprint)
        if matrix.shape[1] < 3:
            return self._insufficient(fingerprint)

        component_count = min(8, max(2, round(math.sqrt(len(usable) / 2))))
        component_count = min(component_count, matrix.shape[0], matrix.shape[1])
        model = NMF(
            n_components=component_count,
            init="nndsvda",
            solver="cd",
            beta_loss="frobenius",
            max_iter=self.config.max_iter,
            random_state=self.config.random_state,
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ConvergenceWarning)
            post_weights = model.fit_transform(matrix)

        feature_names = vectorizer.get_feature_names_out()
        topics = tuple(
            self._topic(cluster_index, component, feature_names)
            for cluster_index, component in enumerate(model.components_)
        )
        assignments: list[TopicAssignmentDraft] = []
        for post, weights in zip(usable, post_weights, strict=True):
            maximum = float(weights.max())
            if maximum <= 0.0:
                ranked = [(0, 1.0)]
            else:
                ranked = sorted(
                    (
                        (cluster_index, float(weight) / maximum)
                        for cluster_index, weight in enumerate(weights)
                    ),
                    key=lambda item: (-item[1], item[0]),
                )
            retained = [ranked[0]]
            retained.extend(
                item for item in ranked[1:3] if item[1] >= self.config.secondary_threshold
            )
            assignments.extend(
                TopicAssignmentDraft(
                    post_id=post.id,
                    cluster_index=cluster_index,
                    rank=rank,
                    normalized_score=score,
                )
                for rank, (cluster_index, score) in enumerate(retained, start=1)
            )
        return TopicModelDraft(
            state=TopicModelState.BUILT,
            config_json=self.config_json,
            corpus_fingerprint=fingerprint,
            topics=topics,
            assignments=tuple(assignments),
        )

    def _insufficient(self, fingerprint: str) -> TopicModelDraft:
        return TopicModelDraft(
            state=TopicModelState.INSUFFICIENT,
            config_json=self.config_json,
            corpus_fingerprint=fingerprint,
            topics=(),
            assignments=(),
        )

    @staticmethod
    def _topic(
        cluster_index: int, component: list[float], feature_names: list[str]
    ) -> DetectedTopicDraft:
        weighted_terms = sorted(
            ((float(weight), str(feature_names[index])) for index, weight in enumerate(component)),
            key=lambda item: (-item[0], item[1]),
        )
        keywords: list[str] = []
        for _, term in weighted_terms:
            if term not in keywords:
                keywords.append(term)
            if len(keywords) == 3:
                break
        return DetectedTopicDraft(
            cluster_index=cluster_index,
            display_label=" · ".join(keywords),
            keywords_json=json.dumps(keywords, ensure_ascii=False, separators=(",", ":")),
        )
