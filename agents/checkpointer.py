"""
Checkpointer factory.

Returns a DynamoDBSaver in production so graph state survives Lambda cold starts
and can be resumed by the FastAPI process. Falls back to MemorySaver for local
mock runs where persistence isn't needed.
"""

import config
from langgraph.checkpoint.memory import MemorySaver


def get_checkpointer():
    if config.USE_MOCK_DATA:
        return MemorySaver()

    from langgraph_checkpoint_dynamodb import DynamoDBSaver
    return DynamoDBSaver(
        client_config={"region_name": config.AWS_REGION},
        checkpoints_table_name=config.DYNAMODB_CHECKPOINT_TABLE,
        writes_table_name=config.DYNAMODB_CHECKPOINT_WRITES_TABLE,
    )
