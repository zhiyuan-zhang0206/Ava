ALTER TABLE agent_notices ADD COLUMN IF NOT EXISTS question JSONB;
ALTER TABLE agent_notices ADD COLUMN IF NOT EXISTS answer JSONB;
ALTER TABLE agent_notices ADD CONSTRAINT agent_notices_question_requires_response
    CHECK (question IS NULL OR (jsonb_typeof(question) = 'object' AND require_response));
ALTER TABLE agent_notices ADD CONSTRAINT agent_notices_answer_requires_question
    CHECK (answer IS NULL OR (jsonb_typeof(answer) = 'object' AND question IS NOT NULL
                              AND resolution IS NOT DISTINCT FROM 'answered'));
