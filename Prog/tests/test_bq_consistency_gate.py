"""
test_bq_consistency_gate.py — Block 2 (2026-08-25, Traditional Learning
BQ34Z110 provisioning /goal): Prog/src/bq_consistency_gate.py tesztjei.

FakePicoSerial-lel (valós Pico/BQ HIL nélkül), a test_bq_data_flash_service.py
mintáját követve.
"""
from __future__ import annotations

import pytest

from Prog.drivers.pico_bq_transport import PicoBQTransport
from Prog.src.battery_profile import BatteryProfile
from Prog.src.bq_consistency_gate import (
    FIELD_TOLERANCES,
    GATE_FIELDS,
    ProvisioningGateDecision,
    build_expected_df_values,
    evaluate_provisioning_consistency,
)
from Prog.src.bq_data_flash_service import (
    FIELD_REGISTRY,
    Bq34z110DataFlashService,
    encode_df_field,
)
from Prog.tests.mock_drivers.fake_pico_serial import FakePicoSerial


def _profile(cell_count: int = 6, nominal_capacity_Ah: float = 7.0) -> BatteryProfile:
    return BatteryProfile(
        battery_name="TEST", manufacturer="TEST", model="TST",
        nominal_voltage_V=2.0 * cell_count, cell_count=cell_count,
        nominal_capacity_Ah=nominal_capacity_Ah, C10_capacity_Ah=nominal_capacity_Ah,
    )


def _blocks_for(values: dict[str, int]) -> dict[tuple[int, int], bytearray]:
    blocks: dict[tuple[int, int], bytearray] = {}
    for name, value in values.items():
        spec = FIELD_REGISTRY[name]
        buf = blocks.setdefault((spec.subclass, spec.block), bytearray(32))
        buf[spec.offset_in_block:spec.offset_in_block + spec.byte_length] = encode_df_field(spec, value)
    return blocks


def _service_for(values: dict[str, int]) -> Bq34z110DataFlashService:
    blocks = _blocks_for(values)

    def _handler(params):
        key = (params["subclass"], params["block"])
        return bytes(blocks.get(key, bytearray(32))).hex()

    transport = PicoBQTransport(serial=FakePicoSerial(results={"data_flash_block": _handler}))
    return Bq34z110DataFlashService(transport)


class TestBuildExpectedDfValues:
    def test_matches_profile_6_cell(self):
        profile = _profile(cell_count=6, nominal_capacity_Ah=7.0)
        values = build_expected_df_values(profile)
        assert values["design_capacity_mAh"] == 7000
        assert values["cell_design_voltage_mV"] == 2000
        # Q-44: kozos profilfuggveny, -2.5 mV/cella/C, 20 C referencia.
        # 5 C: 2437.5 -> 2400; 20 C: 2400; 50 C: 2325 mV.
        # Az abszolut safety-limit tovabbra is 2425 mV/cella.
        assert values["cell_charge_voltage_t1t2_mV"] == 2400  # Q-44, az absmax 2425 marad.
        assert values["cell_charge_voltage_t2t3_mV"] == 2400
        assert values["cell_charge_voltage_t3t4_mV"] == 2325
        assert values["taper_current_mA"] == round(0.03 * 7000)
        assert values["cell_terminate_voltage_mV"] == 1800
        assert values["number_of_series_cells"] == 6

    def test_matches_profile_12_cell(self):
        profile = _profile(cell_count=12, nominal_capacity_Ah=20.0)
        from Prog.src.exceptions import ProfileValidationError

        # Q-22: 24 V × 20 Ah már nem fér a Scale=10 I2 energiamezőbe.
        with pytest.raises(ProfileValidationError, match="Q-22"):
            build_expected_df_values(profile)

    def test_uses_c10_not_nominal_for_design_capacity(self):
        profile = BatteryProfile(
            battery_name="FG", manufacturer="FIAMM", model="FG21202",
            cell_count=6, nominal_capacity_Ah=20.0, C10_capacity_Ah=17.0,
        )
        values = build_expected_df_values(profile)
        assert values["design_capacity_mAh"] == 17000


class TestScf007JeitaTemperatureCompensation:
    """Kozoes profilformula, Q-44 szerinti [2.30, 2.40] toltesi clamp."""

    def test_scales_with_non_default_charge_voltage_without_clamping(self):
        """Q-44 clamp-mentes orakulum: eltero bazis ES tempco."""
        profile = BatteryProfile(
            battery_name="X", manufacturer="TEST", model="X",
            cell_count=6, nominal_capacity_Ah=7.0, C10_capacity_Ah=7.0,
            charge_voltage_per_cell_V=2.38,
            temp_comp_mV_per_cell_per_degC=-1.0,
        )
        values = build_expected_df_values(profile)
        assert values["cell_charge_voltage_t1t2_mV"] == 2395
        assert values["cell_charge_voltage_t2t3_mV"] == 2380
        assert values["cell_charge_voltage_t3t4_mV"] == 2350

    def test_t1t2_clamped_to_safety_ceiling(self):
        """Q-44: a hideg DF-elvaras 2400, az abszolut safety-limit 2425 mV."""
        profile = _profile()
        assert profile.batt_absolute_max_V_per_cell == 2.425
        values = build_expected_df_values(profile)
        assert values["cell_charge_voltage_t1t2_mV"] == 2400

    def test_t3t4_clamped_to_floor_when_raw_value_would_undercut_it(self):
        """2.35 V/cella -- a nyers T3-T4 (2.35-0.075=2.275) a [2.30,2.425]
        clamp ALSÓ határa (2.30V=2300mV) alá esne -- a gate-nek a
        clampelt 2300 mV-ot kell elvárnia, nem a nyers 2275-öt."""
        profile = BatteryProfile(
            battery_name="X", manufacturer="TEST", model="X",
            cell_count=6, nominal_capacity_Ah=7.0, C10_capacity_Ah=7.0,
            charge_voltage_per_cell_V=2.35,
        )
        values = build_expected_df_values(profile)
        assert values["cell_charge_voltage_t3t4_mV"] == 2300

    def test_t1t2_higher_or_equal_t2t3_higher_or_equal_t3t4(self):
        """Fizikai plauzibilitás: hidegebb sávnak NEM LEHET alacsonyabb,
        melegebb sávnak NEM LEHET magasabb kompenzált feszültsége (negatív
        temp-comp együttható) -- a clamp miatt EGYENLŐSÉG is előfordulhat
        (l. fenti clamp-tesztek), de a sorrend megfordulása súlyos,
        safety-releváns regresszió lenne."""
        profile = _profile()
        values = build_expected_df_values(profile)
        assert (
            values["cell_charge_voltage_t1t2_mV"]
            >= values["cell_charge_voltage_t2t3_mV"]
            >= values["cell_charge_voltage_t3t4_mV"]
        )

    def test_t2t3_reference_respects_q44_cap(self):
        """A 20 C-os referencia is legfeljebb 2400 mV/cella."""
        for v in (2.32, 2.35, 2.40, 2.42):
            profile = BatteryProfile(
                battery_name="X", manufacturer="TEST", model="X",
                cell_count=6, nominal_capacity_Ah=7.0, C10_capacity_Ah=7.0,
                charge_voltage_per_cell_V=v,
            )
            values = build_expected_df_values(profile)
            assert values["cell_charge_voltage_t2t3_mV"] == round(min(v, 2.400) * 1000)


class TestEvaluateProvisioningConsistency:
    def test_consistent_when_bq_matches_profile_exactly(self):
        profile = _profile()
        service = _service_for(build_expected_df_values(profile))
        report = evaluate_provisioning_consistency(profile, service)
        assert report.decision is ProvisioningGateDecision.CONSISTENT
        assert report.consistent
        assert len(report.comparisons) == len(GATE_FIELDS)
        assert all(c.within_tolerance for c in report.comparisons)

    def test_consistent_within_tolerance_band(self):
        profile = _profile()
        values = build_expected_df_values(profile)
        values["cell_terminate_voltage_mV"] += FIELD_TOLERANCES["cell_terminate_voltage_mV"]
        service = _service_for(values)
        report = evaluate_provisioning_consistency(profile, service)
        assert report.decision is ProvisioningGateDecision.CONSISTENT

    def test_blocked_when_design_capacity_mismatched_beyond_tolerance(self):
        profile = _profile()
        values = build_expected_df_values(profile)
        values["design_capacity_mAh"] += FIELD_TOLERANCES["design_capacity_mAh"] + 1
        service = _service_for(values)
        report = evaluate_provisioning_consistency(profile, service)
        assert report.decision is ProvisioningGateDecision.BLOCKED_CONFIGURATION_MISMATCH
        assert "design_capacity_mAh" in report.reason
        mismatched = [c for c in report.comparisons if not c.within_tolerance]
        assert [c.field_name for c in mismatched] == ["design_capacity_mAh"]

    def test_blocked_when_cell_count_off_by_one_zero_tolerance(self):
        profile = _profile(cell_count=6)
        values = build_expected_df_values(profile)
        values["number_of_series_cells"] = 5
        service = _service_for(values)
        report = evaluate_provisioning_consistency(profile, service)
        assert report.decision is ProvisioningGateDecision.BLOCKED_CONFIGURATION_MISMATCH
        assert "number_of_series_cells" in report.reason

    def test_blocked_on_readback_failure(self):
        profile = _profile()
        transport = PicoBQTransport(serial=FakePicoSerial(
            results={}, errors={"data_flash_block": ("TIMEOUT", "simulated")},
        ))
        service = Bq34z110DataFlashService(transport)
        report = evaluate_provisioning_consistency(profile, service)
        assert report.decision is ProvisioningGateDecision.BLOCKED_BQ_DF_READBACK_FAILED
        assert report.comparisons == ()

    def test_all_gate_fields_have_tolerances(self):
        assert set(GATE_FIELDS) == set(FIELD_TOLERANCES.keys())

    def test_gate_fields_excludes_learning_mutable_and_cell_bl(self):
        assert "qmax_cell0_initial_mAh" not in GATE_FIELDS
        assert "cell_bl_set_volt_mV" not in GATE_FIELDS
        assert "cell_bh_set_volt_mV" not in GATE_FIELDS

    def test_rejects_legacy_cold_and_uncompensated_hot_bands(self):
        """Q-44: regi 2425 mV hideg DF es fix 2400 mV meleg DF is elter."""
        profile = _profile()
        values = build_expected_df_values(profile)
        values["cell_charge_voltage_t1t2_mV"] = 2425
        values["cell_charge_voltage_t3t4_mV"] = 2400
        report = evaluate_provisioning_consistency(profile, _service_for(values))
        assert report.decision is ProvisioningGateDecision.BLOCKED_CONFIGURATION_MISMATCH
        mismatched = {c.field_name for c in report.comparisons if not c.within_tolerance}
        assert mismatched == {"cell_charge_voltage_t1t2_mV", "cell_charge_voltage_t3t4_mV"}


class TestR16B7EnergyGate:

    @staticmethod
    def literal_service():
        # SLUSB55B Rev.B Table 20 p39–41; FIAMM FG20721 p2.
        # Független, rögzített 24 V/C10/Scale=10 bájtfixture, encoder nélkül.
        blocks = {key: bytearray(32) for key in [(48, 0), (48, 1), (36, 0), (64, 0), (80, 0), (80, 2)]}
        for subclass, block, offset, raw in [
            (48, 0, 6, "07d0"), (48, 0, 21, "1a2c"), (48, 0, 23, "3ed0"),
            (48, 0, 28, "0960"), (48, 0, 30, "0960"), (48, 1, 0, "0915"),  # Q-44: 2400 mV.
            (48, 1, 13, "0a"), (36, 0, 0, "00c9"), (64, 0, 0, "0961"),
            (64, 0, 7, "0c"), (80, 0, 0, "0100"), (80, 2, 3, "0708"),
        ]:
            data = bytes.fromhex(raw)
            blocks[(subclass, block)][offset:offset + len(data)] = data

        def read(params):
            return bytes(blocks[(params["subclass"], params["block"])]).hex()

        return Bq34z110DataFlashService(
            PicoBQTransport(serial=FakePicoSerial(results={"data_flash_block": read})),
        )

    @staticmethod
    def profile(voltage=24.0, c10=6.7, nominal=7.2):
        return BatteryProfile(
            battery_name="FG20721", manufacturer="FIAMM", model="FG20721",
            nominal_voltage_V=voltage, cell_count=6 if voltage == 12.0 else 12,
            nominal_capacity_Ah=nominal, C10_capacity_Ah=c10,
        )

    @pytest.mark.parametrize("voltage,energy,qmax,c10,nominal", [
        (12.0, 8040, 7200, 6.7, 7.2), (24.0, 16080, 7200, 6.7, 7.2),
        (24.0, 32160, 14400, 13.4, 14.4), (12.0, 32160, 28800, 26.8, 28.8),
    ])
    def test_source_values_and_distinct_bases(self, voltage, energy, qmax, c10, nominal):
        from Prog.src.bq_consistency_gate import build_initial_df_values

        # FIAMM FG_FOLDER_EN Rev.11.2025 p2: FG20721 10H=6,7; 20H=7,2 Ah.
        # Q-9: C10 design; Q-23: C20 kezdeti Qmax. A számok rögzített orákulumok.
        profile = self.profile(voltage, c10, nominal)
        values = build_initial_df_values(profile)
        assert values["design_capacity_mAh"] == {7200: 6700, 14400: 13400, 28800: 26800}[qmax]
        assert values["design_energy_cWh"] == energy
        assert values["design_energy_scale"] == 10
        assert values["number_of_series_cells"] == {12.0: 6, 24.0: 12}[voltage]
        assert values["qmax_cell0_initial_mAh"] == qmax
        assert values["load_mode"] == 0
        assert values["load_select"] == 1
        assert "pack_configuration" not in values

    @pytest.mark.parametrize("name,value", [
        ("design_energy_cWh", 1), ("design_energy_scale", 1),
        ("design_energy_scale", 2), ("pack_configuration", 0x09E1),
        ("load_mode", 1), ("load_select", 4),
    ])
    def test_bad_raw_configuration_blocks(self, name, value):
        profile = self.profile()
        service = self.literal_service()
        # Nyers bájtpatch: a hibás Scale=2 a host encoderét is megkerüli.
        subclass_id, block_id, offset, width = {
            "design_energy_cWh": (48, 0, 23, 2), "design_energy_scale": (48, 1, 13, 1),
            "pack_configuration": (64, 0, 0, 2), "load_mode": (80, 0, 1, 1),
            "load_select": (80, 0, 0, 1),
        }[name]
        original_read = service._transport.read_data_flash_block

        def read(subclass, block):
            data = bytearray(original_read(subclass, block))
            if (subclass, block) == (subclass_id, block_id):
                data[offset:offset + width] = value.to_bytes(width, "big")
            return bytes(data)

        service._transport.read_data_flash_block = read
        report = evaluate_provisioning_consistency(profile, service)
        assert report.decision is ProvisioningGateDecision.BLOCKED_CONFIGURATION_MISMATCH
        assert name in report.reason

    @pytest.mark.parametrize("pack_config", [0x0961, 0x4961])
    def test_other_pack_bits_are_preserved(self, pack_config):
        profile = self.profile()
        service = self.literal_service()
        original_read = service._transport.read_data_flash_block

        def read(subclass, block):
            data = bytearray(original_read(subclass, block))
            if (subclass, block) == (64, 0):
                data[0:2] = pack_config.to_bytes(2, "big")
            return bytes(data)

        service._transport.read_data_flash_block = read
        assert evaluate_provisioning_consistency(profile, service).consistent

    @pytest.mark.parametrize("voltage,c10,nominal", [
        (24.0, 13.653, 14.0), (12.0, 27.306, 28.0), (12.0, 32.768, 33.0),
        (24.0, None, 7.2), (12.0, 26.0, 36.0),
    ])
    def test_invalid_profile_blocks_before_read(self, voltage, c10, nominal):
        from unittest.mock import Mock

        service = Mock(wraps=self.literal_service())
        report = evaluate_provisioning_consistency(self.profile(voltage, c10, nominal), service)
        assert not report.consistent
        assert report.comparisons == ()
        service.read_fields.assert_not_called()

    def test_new_block_read_failure_blocks(self):
        profile = self.profile()
        service = self.literal_service()
        original_read = service._transport.read_data_flash_block

        def read(subclass, block):
            if (subclass, block) == (80, 0):
                raise RuntimeError("SC80/0 olvasási hiba")
            return original_read(subclass, block)

        service._transport.read_data_flash_block = read
        report = evaluate_provisioning_consistency(profile, service)
        assert report.decision is ProvisioningGateDecision.BLOCKED_BQ_DF_READBACK_FAILED


class TestR16B7F1NominalEndpoint:
    @pytest.mark.parametrize("nominal,accepted", [(32.767, True), (32.7670001, False)])
    def test_c20_limit_before_rounding_and_readback(self, nominal, accepted):
        from unittest.mock import Mock

        from Prog.src.exceptions import ProfileValidationError

        # Kicsi C10/energia mellett kizárólag a C20/Qmax-bázis határa dönt.
        profile = BatteryProfile(
            battery_name="TEST", manufacturer="TEST", model="TST",
            nominal_capacity_Ah=nominal, C10_capacity_Ah=1.0,
            nominal_voltage_V=2.0, cell_count=1,
        )
        if accepted:
            assert build_expected_df_values(profile)["design_capacity_mAh"] == 1000
        else:
            with pytest.raises(ProfileValidationError, match="Q-22"):
                build_expected_df_values(profile)
            service = Mock()
            report = evaluate_provisioning_consistency(profile, service)
            assert report.decision is ProvisioningGateDecision.BLOCKED_CONFIGURATION_MISMATCH
            service.read_fields.assert_not_called()


@pytest.mark.parametrize("pack_word,x10,decision", [
    (0x0961, 0, ProvisioningGateDecision.CONSISTENT),
    (0x09E1, 1, ProvisioningGateDecision.BLOCKED_CONFIGURATION_MISMATCH),
])
def test_r16b20_x10_readback_uses_bit_units(pack_word, x10, decision):
    profile = _profile()
    values = build_expected_df_values(profile)
    values["pack_configuration"] = pack_word
    report = evaluate_provisioning_consistency(profile, _service_for(values))
    comparison = next(c for c in report.comparisons if c.field_name == "pack_configuration")
    assert report.decision == decision
    assert comparison.profile_expected == 0
    assert comparison.bq_readback == x10
    assert comparison.unit == "bit"
    assert comparison.tolerance == 0
    assert comparison.within_tolerance is (x10 == 0)


def test_r16b25_provisioning_read_preserves_user_abort(monkeypatch):
    from unittest.mock import Mock

    from Prog.src.exceptions import TestAbortedByUser

    profile = _profile()
    service = _service_for(build_expected_df_values(profile))
    aborted = TestAbortedByUser("B25 provisioning read abort")
    read = Mock(side_effect=aborted)
    monkeypatch.setattr(service, "read_fields", read)
    with pytest.raises(TestAbortedByUser) as caught:
        evaluate_provisioning_consistency(profile, service)
    assert caught.value is aborted
    read.assert_called_once_with(GATE_FIELDS)
