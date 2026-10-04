from tools.mapperatorinator_worker import underfilled_sections


def test_dense_neighbor_does_not_hide_a_sparse_section():
    sections=[
        {'start_time':0,'end_time':1000,'retry_min_heads':8},
        {'start_time':1000,'end_time':2000,'retry_min_heads':8},
    ]
    times=[100,200,*[1000+index*40 for index in range(20)]]
    # The aggregate would exceed 16 heads, but the first audible core is still
    # underfilled and must independently trigger its bounded retry.
    assert len(times)>=sum(section['retry_min_heads'] for section in sections)
    sparse=underfilled_sections(times,sections)
    assert sparse==[{**sections[0],'first_heads':2}]


def test_retry_section_windows_are_half_open():
    section={'start_time':0,'end_time':1000,'retry_min_heads':2}
    assert underfilled_sections([100,999,1000],[section])==[]
