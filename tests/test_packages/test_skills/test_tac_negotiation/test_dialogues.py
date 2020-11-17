# -*- coding: utf-8 -*-
# ------------------------------------------------------------------------------
#
#   Copyright 2018-2019 Fetch.AI Limited
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
# ------------------------------------------------------------------------------
"""This module contains the tests of the dialogue classes of the tac control contract skill."""

from pathlib import Path
from typing import cast

import pytest

from aea.exceptions import AEAEnforceError
from aea.helpers.search.models import Description
from aea.helpers.transaction.base import RawTransaction, Terms
from aea.protocols.dialogue.base import DialogueLabel
from aea.test_tools.test_skill import BaseSkillTestCase, COUNTERPARTY_ADDRESS

from packages.fetchai.protocols.contract_api.custom_types import Kwargs
from packages.fetchai.protocols.contract_api.message import ContractApiMessage
from packages.fetchai.protocols.ledger_api.message import LedgerApiMessage
from packages.fetchai.protocols.signing.message import SigningMessage
from packages.fetchai.skills.tac_negotiation.dialogues import (
    ContractApiDialogue,
    ContractApiDialogues,
    FipaDialogue,
    LedgerApiDialogue,
    LedgerApiDialogues,
    SigningDialogue,
    SigningDialogues,
)

from tests.conftest import ROOT_DIR


class TestDialogues(BaseSkillTestCase):
    """Test dialogue classes of tac negotiation."""

    path_to_skill = Path(ROOT_DIR, "packages", "fetchai", "skills", "tac_negotiation")

    @classmethod
    def setup(cls):
        """Setup the test class."""
        super().setup()
        cls.contract_api_dialogues = cast(
            ContractApiDialogues, cls._skill.skill_context.contract_api_dialogues
        )
        cls.signing_dialogues = cast(
            SigningDialogues, cls._skill.skill_context.signing_dialogues
        )
        cls.ledger_api_dialogues = cast(
            LedgerApiDialogues, cls._skill.skill_context.ledger_api_dialogues
        )

    def test_fipa_dialogue(self):
        """Test the FipaDialogue class."""
        fipa_dialogue = FipaDialogue(
            DialogueLabel(
                ("", ""), COUNTERPARTY_ADDRESS, self.skill.skill_context.agent_address,
            ),
            self.skill.skill_context.agent_address,
            role=FipaDialogue.Role.BUYER,
        )

        # counterparty_signature
        with pytest.raises(ValueError, match="counterparty_signature not set!"):
            assert fipa_dialogue.counterparty_signature
        fipa_dialogue.counterparty_signature = "some_counterparty_signature"
        assert fipa_dialogue.counterparty_signature == "some_counterparty_signature"
        with pytest.raises(
            AEAEnforceError, match="counterparty_signature already set!"
        ):
            fipa_dialogue.counterparty_signature = "some_other_counterparty_signature"

        # proposal
        with pytest.raises(ValueError, match="Proposal not set!"):
            assert fipa_dialogue.proposal
        description = Description({"foo1": 1, "bar1": 2})
        fipa_dialogue.proposal = description
        assert fipa_dialogue.proposal == description
        with pytest.raises(AEAEnforceError, match="Proposal already set!"):
            fipa_dialogue.proposal = description

        # terms
        with pytest.raises(ValueError, match="Terms not set!"):
            assert fipa_dialogue.terms
        terms = Terms(
            "some_ledger_id",
            self.skill.skill_context.agent_address,
            "counterprty",
            {"currency_id": 50},
            {"good_id": -10},
            "some_nonce",
        )
        fipa_dialogue.terms = terms
        assert fipa_dialogue.terms == terms
        with pytest.raises(AEAEnforceError, match="Terms already set!"):
            fipa_dialogue.terms = terms

    def test_contract_api_dialogue(self):
        """Test the ContractApiDialogue class."""
        contract_api_dialogue = ContractApiDialogue(
            DialogueLabel(
                ("", ""), COUNTERPARTY_ADDRESS, self.skill.skill_context.agent_address,
            ),
            self.skill.skill_context.agent_address,
            role=ContractApiDialogue.Role.AGENT,
        )

        # associated_fipa_dialogue
        with pytest.raises(ValueError, match="associated_fipa_dialogue not set!"):
            assert contract_api_dialogue.associated_fipa_dialogue

        fipa_dialogue = FipaDialogue(
            DialogueLabel(
                ("", ""), COUNTERPARTY_ADDRESS, self.skill.skill_context.agent_address,
            ),
            self.skill.skill_context.agent_address,
            role=FipaDialogue.Role.BUYER,
        )
        contract_api_dialogue.associated_fipa_dialogue = fipa_dialogue
        with pytest.raises(
            AEAEnforceError, match="associated_fipa_dialogue already set!"
        ):
            contract_api_dialogue.associated_fipa_dialogue = fipa_dialogue
        assert contract_api_dialogue.associated_fipa_dialogue == fipa_dialogue

    def test_contract_api_dialogues(self):
        """Test the ContractApiDialogues class."""
        _, dialogue = self.contract_api_dialogues.create(
            counterparty=COUNTERPARTY_ADDRESS,
            performative=ContractApiMessage.Performative.GET_DEPLOY_TRANSACTION,
            ledger_id="some_ledger_id",
            contract_id="some_contract_id",
            callable="some_callable",
            kwargs=Kwargs({"some_key": "some_value"}),
        )
        assert dialogue.role == ContractApiDialogue.Role.AGENT
        assert dialogue.self_address == self.skill.skill_context.agent_address

    def test_ledger_api_dialogue(self):
        """Test the LedgerApiDialogue class."""
        ledger_api_dialogue = LedgerApiDialogue(
            DialogueLabel(
                ("", ""), COUNTERPARTY_ADDRESS, self.skill.skill_context.agent_address,
            ),
            self.skill.skill_context.agent_address,
            role=ContractApiDialogue.Role.AGENT,
        )

        # associated_signing_dialogue
        with pytest.raises(ValueError, match="Associated signing dialogue not set!"):
            assert ledger_api_dialogue.associated_signing_dialogue
        signing_dialogue = SigningDialogue(
            DialogueLabel(
                ("", ""), COUNTERPARTY_ADDRESS, self.skill.skill_context.agent_address,
            ),
            self.skill.skill_context.agent_address,
            role=SigningDialogue.Role.SKILL,
        )
        ledger_api_dialogue.associated_signing_dialogue = signing_dialogue
        with pytest.raises(
            AEAEnforceError, match="Associated signing dialogue already set!"
        ):
            ledger_api_dialogue.associated_signing_dialogue = signing_dialogue
        assert ledger_api_dialogue.associated_signing_dialogue == signing_dialogue

    def test_ledger_api_dialogues(self):
        """Test the LedgerApiDialogues class."""
        _, dialogue = self.ledger_api_dialogues.create(
            counterparty=COUNTERPARTY_ADDRESS,
            performative=LedgerApiMessage.Performative.GET_BALANCE,
            ledger_id="some_ledger_id",
            address="some_address",
        )
        assert dialogue.role == LedgerApiDialogue.Role.AGENT
        assert dialogue.self_address == self.skill.skill_context.agent_address

    def test_signing_dialogue(self):
        """Test the SigningDialogue class."""
        signing_dialogue = SigningDialogue(
            DialogueLabel(
                ("", ""), COUNTERPARTY_ADDRESS, self.skill.skill_context.agent_address,
            ),
            self.skill.skill_context.agent_address,
            role=ContractApiDialogue.Role.AGENT,
        )

        # associated_contract_api_dialogue
        with pytest.raises(ValueError, match="associated_fipa_dialogue not set!"):
            assert signing_dialogue.associated_fipa_dialogue
        fipa_dialogue = FipaDialogue(
            DialogueLabel(
                ("", ""), COUNTERPARTY_ADDRESS, self.skill.skill_context.agent_address,
            ),
            self.skill.skill_context.agent_address,
            role=FipaDialogue.Role.BUYER,
        )
        signing_dialogue.associated_fipa_dialogue = fipa_dialogue
        with pytest.raises(
            AEAEnforceError, match="associated_fipa_dialogue already set!"
        ):
            signing_dialogue.associated_fipa_dialogue = fipa_dialogue
        assert signing_dialogue.associated_fipa_dialogue == fipa_dialogue

    def test_signing_dialogues(self):
        """Test the SigningDialogues class."""
        _, dialogue = self.signing_dialogues.create(
            counterparty=COUNTERPARTY_ADDRESS,
            performative=SigningMessage.Performative.SIGN_TRANSACTION,
            terms=Terms(
                "some_ledger_id",
                "some_sender_address",
                "some_counterparty_address",
                dict(),
                dict(),
                "some_nonce",
            ),
            raw_transaction=RawTransaction("some_ledger_id", "some_body"),
        )
        assert dialogue.role == SigningDialogue.Role.SKILL
        assert dialogue.self_address == str(self.skill.skill_context.skill_id)
