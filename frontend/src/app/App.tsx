import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Layout, Menu, Space, Tag, Typography } from "antd";
import {
  AuditOutlined,
  BookOutlined,
  DeploymentUnitOutlined,
  ExportOutlined,
  FileSearchOutlined,
  RocketOutlined,
  UserOutlined,
} from "@ant-design/icons";
import { api } from "../shared/api/client";
import type { Novel } from "../shared/types/api";
import { NovelsPage } from "../features/novels/NovelsPage";
import { JobsPage } from "../features/jobs/JobsPage";
import { CharactersPage } from "../features/characters/CharactersPage";
import { GraphPage } from "../features/graph/GraphPage";
import { ReviewsPage } from "../features/reviews/ReviewsPage";
import { AnalysesPage } from "../features/analyses/AnalysesPage";
import { ExportsPage } from "../features/exports/ExportsPage";
import { EvidenceDrawer } from "../features/evidence/EvidenceDrawer";

const { Header, Sider, Content } = Layout;

export function App() {
  const [activeMenu, setActiveMenu] = useState("novels");
  const [selectedNovel, setSelectedNovel] = useState<Novel | null>(null);
  const [evidenceId, setEvidenceId] = useState<string | null>(null);
  const [graphCenterCharId, setGraphCenterCharId] = useState<string | null>(null);

  const healthQuery = useQuery({
    queryKey: ["health"],
    queryFn: api.healthReady,
    retry: false,
  });

  const novelId = selectedNovel?.id || null;

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Sider width={220} theme="dark">
        <div style={{ padding: "18px 16px", color: "#fff", fontWeight: 700, fontSize: 16 }}>
          NCKE 知识引擎
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
            { key: "exports", icon: <ExportOutlined />, label: "数据导出" },
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
          {activeMenu === "exports" && <ExportsPage novelId={novelId} />}
        </Content>
      </Layout>

      <EvidenceDrawer evidenceId={evidenceId} onClose={() => setEvidenceId(null)} />
    </Layout>
  );
}
