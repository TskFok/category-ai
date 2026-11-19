import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Input, Layout, Menu, message, Modal, Space, Tag, Typography } from "antd";
import {
  AudioOutlined,
  AuditOutlined,
  BookOutlined,
  CustomerServiceOutlined,
  DeploymentUnitOutlined,
  ExportOutlined,
  FileSearchOutlined,
  FileTextOutlined,
  KeyOutlined,
  OrderedListOutlined,
  RocketOutlined,
  SoundOutlined,
  UserOutlined,
} from "@ant-design/icons";
import { api, getAuthToken, setAuthToken } from "../shared/api/client";
import type { Novel } from "../shared/types/api";
import { NovelsPage } from "../features/novels/NovelsPage";
import { JobsPage } from "../features/jobs/JobsPage";
import { CharactersPage } from "../features/characters/CharactersPage";
import { GraphPage } from "../features/graph/GraphPage";
import { ReviewsPage } from "../features/reviews/ReviewsPage";
import { AnalysesPage } from "../features/analyses/AnalysesPage";
import { ExportsPage } from "../features/exports/ExportsPage";
import { EvidenceDrawer } from "../features/evidence/EvidenceDrawer";
import { EpisodesPage } from "../features/episodes/EpisodesPage";
import { VoicesPage } from "../features/voices/VoicesPage";
import { ScriptsPage } from "../features/scripts/ScriptsPage";
import { AudioStudioPage } from "../features/audio/AudioStudioPage";
import { AudioExportsPage } from "../features/audio_exports/AudioExportsPage";

const { Header, Sider, Content } = Layout;

export function App() {
  const qc = useQueryClient();
  const [activeMenu, setActiveMenu] = useState("novels");
  const [selectedNovel, setSelectedNovel] = useState<Novel | null>(null);
  const [evidenceId, setEvidenceId] = useState<string | null>(null);
  const [graphCenterCharId, setGraphCenterCharId] = useState<string | null>(null);
  const [selectedEpisodeId, setSelectedEpisodeId] = useState<string | null>(null);
  const [authModalOpen, setAuthModalOpen] = useState(false);
  const [tokenInput, setTokenInput] = useState(getAuthToken());

  const healthQuery = useQuery({
    queryKey: ["health"],
    queryFn: api.healthReady,
    retry: false,
  });

  const novelId = selectedNovel?.id || null;

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Sider width={230} theme="dark">
        <div style={{ padding: "18px 16px", color: "#fff", fontWeight: 700, fontSize: 16 }}>
          Category AI 有声引擎
        </div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[activeMenu]}
          onClick={({ key }) => setActiveMenu(key)}
          items={[
            { key: "novels", icon: <BookOutlined />, label: "小说项目" },
            { key: "jobs", icon: <RocketOutlined />, label: "分析任务中心" },
            { key: "characters", icon: <UserOutlined />, label: "人物档案" },
            { key: "graph", icon: <DeploymentUnitOutlined />, label: "人物关系图谱" },
            { key: "analyses", icon: <FileSearchOutlined />, label: "深度分析报告" },
            { key: "reviews", icon: <AuditOutlined />, label: "身份审核工作台" },
            { type: "divider" },
            { key: "episodes", icon: <OrderedListOutlined />, label: "分集规划" },
            { key: "voices", icon: <AudioOutlined />, label: "配音角色" },
            { key: "scripts", icon: <FileTextOutlined />, label: "配音剧本" },
            { key: "audio", icon: <CustomerServiceOutlined />, label: "音频制作" },
            { key: "audio_exports", icon: <SoundOutlined />, label: "音频导出" },
            { type: "divider" },
            { key: "exports", icon: <ExportOutlined />, label: "知识数据导出" },
          ]}
        />
      </Sider>

      <Layout>
        <Header
          style={{
            background: "#fff",
            padding: "0 24px",
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            borderBottom: "1px solid #f0f0f0",
          }}
        >
          <Space>
            <Typography.Text strong>当前项目：</Typography.Text>
            {selectedNovel ? (
              <Tag color="blue">
                {selectedNovel.title} ({selectedNovel.chapter_count} 章)
              </Tag>
            ) : (
              <Typography.Text type="secondary">未选择小说</Typography.Text>
            )}
          </Space>
          <Space>
            <Button
              size="small"
              icon={<KeyOutlined />}
              onClick={() => {
                setTokenInput(getAuthToken());
                setAuthModalOpen(true);
              }}
            >
              {getAuthToken() ? "已配置令牌" : "访问令牌"}
            </Button>
            <Tag color={healthQuery.data?.status === "ok" ? "green" : "orange"}>
              API: {healthQuery.data?.status || "connecting"}
            </Tag>
          </Space>
        </Header>

        <Content style={{ padding: 24, background: "#f5f7fa" }}>
          {activeMenu === "novels" && (
            <NovelsPage
              selectedNovelId={novelId}
              onSelectNovel={(n) => setSelectedNovel(n)}
            />
          )}
          {activeMenu === "jobs" && <JobsPage novelId={novelId} />}
          {activeMenu === "characters" && (
            <CharactersPage
              novelId={novelId}
              onOpenEvidence={setEvidenceId}
              onOpenGraphForCharacter={(cid) => {
                setGraphCenterCharId(cid);
                setActiveMenu("graph");
              }}
            />
          )}
          {activeMenu === "graph" && (
            <GraphPage
              novelId={novelId}
              initialCenterId={graphCenterCharId}
              onOpenEvidence={setEvidenceId}
            />
          )}
          {activeMenu === "analyses" && (
            <AnalysesPage novelId={novelId} onOpenEvidence={setEvidenceId} />
          )}
          {activeMenu === "reviews" && <ReviewsPage novelId={novelId} />}
          {activeMenu === "episodes" && (
            <EpisodesPage
              novelId={novelId}
              onSelectEpisodeForScript={(epId) => {
                setSelectedEpisodeId(epId);
                setActiveMenu("scripts");
              }}
            />
          )}
          {activeMenu === "voices" && <VoicesPage novelId={novelId} />}
          {activeMenu === "scripts" && (
            <ScriptsPage novelId={novelId} initialEpisodeId={selectedEpisodeId} />
          )}
          {activeMenu === "audio" && <AudioStudioPage novelId={novelId} />}
          {activeMenu === "audio_exports" && <AudioExportsPage novelId={novelId} />}
          {activeMenu === "exports" && <ExportsPage novelId={novelId} />}
        </Content>
      </Layout>

      <EvidenceDrawer evidenceId={evidenceId} onClose={() => setEvidenceId(null)} />

      <Modal
        title="配置访问令牌 (Bearer Token)"
        open={authModalOpen}
        onCancel={() => setAuthModalOpen(false)}
        onOk={() => {
          setAuthToken(tokenInput);
          setAuthModalOpen(false);
          qc.invalidateQueries();
          message.success(tokenInput.trim() ? "访问令牌已保存至当前会话" : "已清除访问令牌");
        }}
      >
        <Space direction="vertical" style={{ width: "100%" }}>
          <Typography.Text type="secondary">
            生产环境需要服务端分配的访问令牌（切勿在此输入 DeepSeek 或 CosyVoice API Key）。
          </Typography.Text>
          <Input.Password
            value={tokenInput}
            onChange={(e) => setTokenInput(e.target.value)}
            placeholder="输入 ADMIN / REVIEWER / READER 令牌"
          />
        </Space>
      </Modal>
    </Layout>
  );
}
