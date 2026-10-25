from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TTSParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(default="", max_length=3000)
    voice: str = Field(default="en", min_length=1, max_length=32, pattern=r"^[A-Za-z0-9][A-Za-z0-9_+\-]*$")
    rate: int = Field(default=170, ge=80, le=300)
    pitch: int = Field(default=50, ge=0, le=99)
    volume: int = Field(default=100, ge=0, le=200)


class ProjectFields(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=200)
    aspect_ratio: Literal["16:9", "9:16"] = "9:16"
    duration_seconds: float = Field(default=5, ge=1, le=120, allow_inf_nan=False)
    tts: TTSParameters = Field(default_factory=TTSParameters)
    render_prompt: str = Field(default="Gentle cinematic camera motion, preserve the original subject.", min_length=1, max_length=2000)


class ProjectPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    aspect_ratio: Literal["16:9", "9:16"] | None = None
    duration_seconds: float | None = Field(default=None, ge=1, le=120, allow_inf_nan=False)
    tts: TTSParameters | None = None
    render_prompt: str | None = Field(default=None, min_length=1, max_length=2000)

    @model_validator(mode="after")
    def reject_null(self):
        for field in self.model_fields_set - {"version"}:
            if getattr(self, field) is None:
                raise ValueError("explicit null is not allowed")
        return self


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID
    request_id: UUID = Field(default_factory=uuid4, description="再送時にも同じUUIDを使用すると重複生成を防げます。")
    provider: Literal["auto", "replicate", "runway", "kling"] = "auto"


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    acknowledge_unknown: bool = False


class ScriptGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    raw_input: str = Field(min_length=1, max_length=12_000)
    scene_count: int = Field(default=4, ge=1, le=8)


class ScriptScene(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    scene_number: int = Field(ge=1, le=8)
    duration_seconds: int = Field(ge=1, le=60)
    narration: str = Field(min_length=1, max_length=250)
    visual_prompt: str = Field(min_length=1, max_length=1_500)


class ScriptDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=200)
    visual_prompt: str = Field(min_length=1, max_length=2_000)
    scenes: list[ScriptScene] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def narration_fits_video_input(self):
        if sum(len(scene.narration) for scene in self.scenes) + max(0, len(self.scenes) - 1) > 1_000:
            raise ValueError("combined narration exceeds the video input limit")
        total_duration = sum(scene.duration_seconds for scene in self.scenes)
        if not 15 <= total_duration <= 30:
            raise ValueError("combined scene duration must be between 15 and 30 seconds")
        return self
