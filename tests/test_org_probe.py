import copy
import json
from unittest.mock import patch

import pytest

from claude_swap.org_probe import collect_org
from claude_swap.token_runtime import TokenRuntime, capabilities, command
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.models import Platform
from claude_swap.token_probe import iso

ID = '11111111-1111-4111-8111-111111111111'
NOW = 2000000000


@pytest.fixture
def setup(temp_home):
    switcher = ClaudeAccountSwitcher()
    switcher.platform = Platform.LINUX
    switcher.add_account_from_token(token='sk-ant-oat01-org-fixture', email='managed-'+ID+'@setup-token.local')
    data = switcher._get_sequence_data()
    data['accounts']['1'].update(managedAccountId=ID, credentialType='setup_token', credentialGeneration=4)
    switcher._write_json(switcher.sequence_file, data)
    runtime = TokenRuntime(switcher, clock=lambda: NOW)
    ref = runtime.status()['accounts'][0]['accountRef']
    permit = dict(version=1, permitId='22222222-2222-4222-8222-222222222222',companyId='company',machineId='machine',managedAccountId=ID,credentialGeneration=4,policyRevision=2,reservedAt=NOW*1000,transportDeadline=NOW*1000+10000,expiresAt=NOW*1000+30000,timeoutMs=10000,accountRemaining=95,companyRemaining=287)
    context = dict(companyId='company',machineId='machine',managedAccountId=ID,credentialGeneration=4,policyRevision=2,accountRef=ref)
    request = dict(version=1,context=context,permit=permit,inUse=True,online=True)
    return runtime, request


def observation():
    return dict(version=1,source='inference_probe',observedAt=iso(NOW),windows=[dict(kind='unified5h',pct=63,resetsAt=iso(NOW+1000),status=None)],coverage='unknown',reason='coverage_unknown',retryAt=None,authState='usable')


def test_managed_one_use_grant_calls_without_local_consent_and_replay_is_durable(setup):
    runtime, request = setup
    assert capabilities()['organizationCollectorVersion'] == 1
    with patch('claude_swap.org_probe.probe',return_value=observation()) as http:
        result = collect_org(runtime,request)
        assert result['observation']['credentialGeneration'] == 4
        assert result['observation']['accountRef'] == request['context']['accountRef']
        again = TokenRuntime(runtime.switcher,clock=runtime.clock)
        assert collect_org(again,request)['reason'] == 'permit_replayed'
        http.assert_called_once()
    assert runtime.status()['budget']['machineUsed24h'] == 1
    assert runtime.status()['accounts'][0]['probeEnabled'] is False
    assert 'sk-ant-oat01-' not in runtime.state_file.read_text()
    assert 'sk-ant-oat01-' not in json.dumps(result)


@pytest.mark.parametrize('field,value',[('companyId','other'),('machineId','other'),('managedAccountId','33333333-3333-4333-8333-333333333333'),('credentialGeneration',3),('policyRevision',1),('version',2),('timeoutMs',20000),('expiresAt',NOW*1000+60000),('reservedAt',NOW*1000+1000),('transportDeadline',NOW*1000-1)])
def test_invalid_scope_generation_policy_time_grant_never_calls(setup,field,value):
    runtime,request=setup
    request['permit'][field]=value
    with patch('claude_swap.org_probe.probe') as http:
        assert collect_org(runtime,request)['reason'] == 'grant_invalid'
        http.assert_not_called()


@pytest.mark.parametrize('field',['inUse','online'])
def test_receiver_unused_offline_no_fallback(setup,field):
    runtime,request=setup
    request[field]=False
    with patch('claude_swap.org_probe.probe') as http:
        assert collect_org(runtime,request)['reason'] in ('offline','not_in_use')
        http.assert_not_called()


def test_late_credential_replacement_discards_result_spending_remains(setup):
    runtime,request=setup
    def http(*args,**kwargs):
        runtime.switcher._write_account_credentials('1',runtime.switcher.account_email('1'),json.dumps({'claudeAiOauth':{'accessToken':'sk-ant-oat01-new-fixture','scopes':['user:inference']}}))
        return observation()
    with patch('claude_swap.org_probe.probe',side_effect=http):
        assert collect_org(runtime,request)['reason']=='credential_changed'
    assert runtime.status()['budget']['machineUsed24h']==1


def test_transit_reduces_timeout_and_post_deadline_discards(setup):
    runtime,request=setup
    runtime.clock=lambda:NOW+8
    with patch('claude_swap.org_probe.probe',return_value=observation()) as http:
        collect_org(runtime,request)
        assert http.call_args.kwargs['timeout_s']==2
    runtime,request=setup
    request=copy.deepcopy(request)
    request['permit']['permitId']='44444444-4444-4444-8444-444444444444'
    runtime.clock=lambda:NOW+1000
    request['permit'].update(reservedAt=(NOW+1000)*1000,transportDeadline=(NOW+1010)*1000,expiresAt=(NOW+1030)*1000)
    def slow(*args,**kwargs):
        runtime.clock=lambda:NOW+1011
        return observation()
    with patch('claude_swap.org_probe.probe',side_effect=slow):
        assert collect_org(runtime,request)['reason']=='permit_expired'


def test_grant_write_failure_prevents_http(setup):
    runtime,request=setup
    with patch.object(runtime,'_save',side_effect=OSError('fixture')),patch('claude_swap.org_probe.probe') as http:
        with pytest.raises(OSError):collect_org(runtime,request)
        http.assert_not_called()


def test_cli_grant_only_stdin_and_bounded(monkeypatch):
    import io
    monkeypatch.setattr('sys.stdin',io.StringIO('x'*8193))
    with pytest.raises(ValueError,match='grant_invalid'):command(['collect-org'])
