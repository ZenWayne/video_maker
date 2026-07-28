from app.models.schemas import ContentAnalysisResponse, ReferenceSampleResponse


def test_response_from_attributes():
    class FakeSample:
        id = 3; analysis_id = "A1"; order_index = 0
        video_path = "/x/0.mp4"; has_speech = True
        hook_text = "wait for it"; full_transcript = "wait for it, here is why"
        language = "en"; status = "transcribed"; error_message = None
        from datetime import datetime; created_at = datetime.utcnow()

    class FakeAnalysis:
        id = "A1"; title = "t"; region_hint = "en"; status = "completed"
        brief_json = '{"niche_summary":"x"}'; error_message = None
        from datetime import datetime
        created_at = datetime.utcnow(); updated_at = datetime.utcnow()
        samples = [FakeSample()]

    r = ContentAnalysisResponse.model_validate(FakeAnalysis())
    assert r.id == "A1"
    assert r.samples[0].hook_text == "wait for it"
    assert r.brief_json == '{"niche_summary":"x"}'
