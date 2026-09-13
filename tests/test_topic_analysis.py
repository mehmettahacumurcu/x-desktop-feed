import json
from dataclasses import asdict

import pytest

from xfeed.topic_analysis import (
    AnalysisPost,
    TopicAnalysisConfig,
    TopicAnalyzer,
    TopicModelState,
    normalize_text,
    tokenize,
)


def post(post_id: int, text: str, *, updated_at: str = "2026-08-01 12:00:00") -> AnalysisPost:
    return AnalysisPost(id=post_id, text=text, updated_at=updated_at)


@pytest.fixture
def corpus() -> tuple[AnalysisPost, ...]:
    themes = (
        "python package typing async pytest desktop application",
        "sqlite database query index transaction schema desktop",
        "mutfak tarif yemek lezzet sebze pişirme sofra",
    )
    return tuple(post(index + 1, f"{themes[index % len(themes)]} örnek") for index in range(24))


def test_normalization_removes_urls_handles_and_stop_words_but_retains_hashtag_text() -> None:
    text = "ＴＨＥ and is ve bir bu için #Python #çalışma https://example.com/x @someone"

    assert normalize_text(text) == ("the and is ve bir bu için  python  çalışma    ")
    assert tokenize(text) == ["python", "çalışma"]


def test_configuration_serialization_is_stable_and_complete() -> None:
    config = TopicAnalysisConfig()
    result = TopicAnalyzer(config).analyze(tuple(post(i, "python desktop app") for i in range(9)))

    assert result.config_json == json.dumps(asdict(config), sort_keys=True, separators=(",", ":"))
    assert result.metadata.method == "tfidf_nmf"
    assert result.metadata.method_version == "1"
    assert result.metadata.config_json == result.config_json
    assert result.metadata.corpus_fingerprint == result.corpus_fingerprint


def test_analyzer_metadata_uses_current_configuration_and_given_fingerprint() -> None:
    analyzer = TopicAnalyzer(TopicAnalysisConfig(random_state=7))

    metadata = analyzer.metadata("corpus-sha")

    assert metadata.method == "tfidf_nmf"
    assert metadata.method_version == "1"
    assert metadata.config_json == analyzer.config_json
    assert metadata.corpus_fingerprint == "corpus-sha"


def test_fewer_than_ten_usable_posts_returns_insufficient() -> None:
    result = TopicAnalyzer().analyze(tuple(post(i, f"python desktop app {i}") for i in range(9)))

    assert result.state is TopicModelState.INSUFFICIENT
    assert result.topics == ()
    assert result.assignments == ()


def test_posts_without_meaningful_tokens_do_not_count_as_usable() -> None:
    posts = tuple(post(i, f"python desktop app {i}") for i in range(9)) + (
        post(10, "the ve bir https://example.com @someone"),
    )

    assert TopicAnalyzer().analyze(posts).state is TopicModelState.INSUFFICIENT


def test_whitespace_only_posts_do_not_change_the_analyzed_model_or_fingerprint(
    corpus: tuple[AnalysisPost, ...],
) -> None:
    analyzer = TopicAnalyzer()

    baseline = analyzer.analyze(corpus)
    with_whitespace = analyzer.analyze((*corpus, post(999, " \t\n ")))

    assert with_whitespace == baseline


def test_analysis_is_deterministic_and_stores_at_most_three_ranked_assignments(
    corpus: tuple[AnalysisPost, ...],
) -> None:
    first = TopicAnalyzer().analyze(corpus)
    second = TopicAnalyzer().analyze(tuple(reversed(corpus)))

    assert first == second
    assert first.state is TopicModelState.BUILT
    grouped: dict[int, list[object]] = {}
    for assignment in first.assignments:
        grouped.setdefault(assignment.post_id, []).append(assignment)
    assert set(grouped) == {item.id for item in corpus}
    for assignments in grouped.values():
        assert [item.rank for item in assignments] == list(range(1, len(assignments) + 1))
        assert 1 <= len(assignments) <= 3
        assert assignments[0].normalized_score == pytest.approx(1.0)
        assert all(item.normalized_score >= 0.35 for item in assignments[1:])


def test_topic_labels_are_stable_ranked_nonduplicate_terms(
    corpus: tuple[AnalysisPost, ...],
) -> None:
    result = TopicAnalyzer().analyze(corpus)

    assert [topic.cluster_index for topic in result.topics] == list(range(len(result.topics)))
    for topic in result.topics:
        keywords = json.loads(topic.keywords_json)
        assert len(keywords) == 3
        assert len(keywords) == len(set(keywords))
        assert topic.display_label == " · ".join(keywords)


def test_ten_usable_posts_with_fewer_than_three_features_returns_insufficient() -> None:
    corpus = tuple(post(index + 1, "python" if index < 5 else "sqlite") for index in range(10))

    result = TopicAnalyzer().analyze(corpus)

    assert result.state is TopicModelState.INSUFFICIENT
    assert result.topics == ()
    assert result.assignments == ()


def test_topic_count_uses_configured_corpus_size_formula() -> None:
    themes = (
        "python package typing async pytest",
        "sqlite database query index transaction",
        "mutfak tarif yemek lezzet sebze",
    )
    corpus = tuple(post(i + 1, themes[i % 3]) for i in range(18))

    result = TopicAnalyzer().analyze(corpus)

    assert len(result.topics) == 3


def test_corpus_fingerprint_changes_with_content_timestamp_or_configuration(
    corpus: tuple[AnalysisPost, ...],
) -> None:
    baseline = TopicAnalyzer().analyze(corpus)
    changed_text = TopicAnalyzer().analyze(
        (post(corpus[0].id, "different", updated_at=corpus[0].updated_at), *corpus[1:])
    )
    changed_timestamp = TopicAnalyzer().analyze(
        (post(corpus[0].id, corpus[0].text, updated_at="changed"), *corpus[1:])
    )
    changed_config = TopicAnalyzer(TopicAnalysisConfig(random_state=7)).analyze(corpus)

    assert baseline.corpus_fingerprint != changed_text.corpus_fingerprint
    assert baseline.corpus_fingerprint != changed_timestamp.corpus_fingerprint
    assert baseline.corpus_fingerprint != changed_config.corpus_fingerprint
