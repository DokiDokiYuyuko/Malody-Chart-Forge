from malody_studio.density_lift_guard import full_cap_violations, local_cap_allows


def test_sparse_half_second_cannot_triple():
    base = [100.0]
    assert local_cap_allows(base, [], 100.0)
    assert not local_cap_allows(base, [100.0], 200.0)
    assert full_cap_violations(base, [100.0, 100.0, 200.0])


def test_local_cap_rejects_cluster_even_when_global_increase_is_small():
    base = [float(i * 1000) for i in range(20)]
    assert local_cap_allows(base, [], 1000.0)
    assert not local_cap_allows(base, [1000.0], 1000.0)
    assert not full_cap_violations(base, base)
