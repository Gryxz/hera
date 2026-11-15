from hera.privacy.grants import GrantStore


def test_create_all_revoke(conn):
    gs = GrantStore(conn)
    assert gs.all() == []
    g = gs.create(content_hashes=["h1", "h2"], destinations=["provider-a"], purpose="implement",
                  expires_at=2000.0, grant_id="g1")
    auto = gs.create(content_hashes={"h3"}, destinations={"provider-b"}, purpose="diagnose",
                     expires_at=3000)
    assert auto.grant_id and auto.grant_id != "g1"
    assert g.content_hashes == frozenset({"h1", "h2"})
    stored = {x.grant_id: x for x in gs.all()}
    assert stored["g1"] == g and stored[auto.grant_id] == auto
    gs.revoke("g1")
    assert [x.grant_id for x in gs.all()] == [auto.grant_id]
    gs.revoke("g1")                      # revoking again is harmless
    assert len(gs.all()) == 1


def test_module_states_local_only_rule():
    from hera.privacy import grants
    assert "local user command" in grants.__doc__
