"""Executable schedule witness for the synthetic two-scenario fixture."""
import json
from pathlib import Path


def test_fixed_roster_witness_and_capacity_lower_bound():
    root=Path(__file__).parents[1]
    data=json.loads((root/"fixtures/support/surge.json").read_text())
    base=data["agents"]
    oncall=data["oncall_options"]
    assert sum(a["shift_end"]-a["shift_start"] for a in base)==5
    assert sum(c["resolve_minutes"] for c in data["cases"])+2==9
    assert all(5+(a["shift_end"]-a["shift_start"])<9 for a in oncall)
    assert sum(a["reserve_cost"] for a in oncall)==8
    agents={a["agent_id"]:a for a in base+oncall}
    for scenario in data["surge_scenarios"]:
        cases={c["case_id"]:c for c in data["cases"]+[scenario["case"]]}
        if scenario["scenario_id"]=="shipping_surge":
            response={"s-1":("a-1",0,1),"s-2":("a-2",0,1),"s-3":("a-1",1,2),"s-4":("a-2",1,2)}
            resolution={"s-1":("a-1",0,3),"s-2":("a-2",0,2),"s-3":("a-3",1,3),"s-4":("a-4",1,3)}
        else:
            response={"s-1":("a-1",0,1),"s-2":("a-2",0,1),"s-3":("a-1",1,2),"s-5":("a-1",2,3)}
            resolution={"s-1":("a-1",0,3),"s-2":("a-2",0,2),"s-3":("a-3",1,3),"s-5":("a-4",1,3)}
        for schedule,field in ((response,"response_minutes"),(resolution,"resolve_minutes")):
            assert set(schedule)==set(cases)
            for case_id,(agent_id,start,end) in schedule.items():
                c=cases[case_id]; a=agents[agent_id]
                assert end-start==c[field]
                assert c["arrives"]<=start<end<=c["deadline"]
                assert a["shift_start"]<=start<end<=a["shift_end"]
                assert c["skill"] in a["skills"] and c["language"] in a["languages"]
            assert all(not (x[0]==y[0] and max(x[1],y[1])<min(x[2],y[2]))
                       for i,x in enumerate(schedule.values()) for y in list(schedule.values())[i+1:])
    corrected=5+(2-1)+(3-1)
    assert corrected==8<9
