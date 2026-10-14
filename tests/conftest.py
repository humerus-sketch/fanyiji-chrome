"""Collect only tests belonging to this independent Chrome application.

These files came from the original combined project's test tree. Their subject
modules/tools intentionally do not exist in the Chrome edition. Keep this list
explicit so failures in the supported Chrome/queue/GTCRN code remain visible.
"""
collect_ignore = [
    "test_asr_byte_output.py",
    "test_asr_models.py",
    "test_asr_pair_summary.py",
    "test_audio_processing.py",
    "test_browser_dialog.py",
    "test_evaluate_all_asr.py",
    "test_evaluate_all_translation.py",
    "test_evaluate_asr_accuracy.py",
    "test_evaluate_codex_models.py",
    "test_hybrid_ui.py",
    "test_model_import.py",
    "test_mt_resource_query.py",
    "test_prepare_asr_candidates.py",
    "test_prepare_danmaku_wordlist.py",
    "test_reference_filter_scope.py",
    "test_specialized_integration.py",
    "test_web_translation_ui.py",
]
