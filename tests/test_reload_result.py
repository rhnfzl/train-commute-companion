from runtime_rig import started

from train_commute_companion.messages import RefusalKind, Refused


async def test_reload_returns_refusal_when_old_profiles_remain_active(tmp_path):
    rig = await started(tmp_path)
    before = rig.rt.config
    rig.write(text="profiles: not-a-list")
    result = await rig.rt.reload()
    assert isinstance(result, Refused)
    assert rig.rt.config == before


async def test_reload_returns_retry_when_new_profiles_are_not_durable(tmp_path):
    rig = await started(tmp_path)
    before = rig.rt.config
    rig.write("renamed")
    rig.store.fail = True
    result = await rig.rt.reload()
    assert isinstance(result, Refused) and result.kind is RefusalKind.RETRY
    assert rig.rt.config == before


async def test_reload_returns_success_after_adopting_profiles(tmp_path):
    rig = await started(tmp_path)
    rig.write("renamed")
    assert await rig.rt.reload() is None
    assert rig.rt.config.profiles[0].name == "renamed"
