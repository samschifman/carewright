import { useState } from "react";
import { NavLink, useLocation } from "react-router-dom";
import {
  Button,
  Content,
  Masthead,
  MastheadBrand,
  MastheadContent,
  MastheadMain,
  MastheadToggle,
  Nav,
  NavItem,
  NavList,
  Page,
  PageSidebar,
  PageSidebarBody,
  SkipToContent,
  Toolbar,
  ToolbarContent,
  ToolbarItem,
} from "@patternfly/react-core";
import { BarsIcon, MoonIcon, SunIcon } from "@patternfly/react-icons";
import { useTheme } from "./ThemeProvider";

export interface NavItemConfig {
  label: string;
  path: string;
  icon?: React.ReactNode;
}

export interface AppShellProps {
  navItems: NavItemConfig[];
  brandText?: string;
  children: React.ReactNode;
  headerActions?: React.ReactNode;
}

export function AppShell({
  navItems,
  brandText = "CPG Care Plans",
  children,
  headerActions,
}: AppShellProps) {
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const { theme, toggleTheme } = useTheme();
  const location = useLocation();

  const nav = (
    <Nav aria-label="Application navigation">
      <NavList>
        {navItems.map((item) => (
          <NavItem
            key={item.path}
            isActive={location.pathname === item.path}
          >
            <NavLink to={item.path}>
              {item.icon && <span style={{ marginRight: "0.5rem" }}>{item.icon}</span>}
              {item.label}
            </NavLink>
          </NavItem>
        ))}
      </NavList>
    </Nav>
  );

  const masthead = (
    <Masthead>
      <MastheadToggle>
        <Button
          variant="plain"
          onClick={() => setSidebarOpen((prev) => !prev)}
          aria-label="Toggle sidebar"
        >
          <BarsIcon />
        </Button>
      </MastheadToggle>
      <MastheadMain>
        <MastheadBrand>
          <Content component="h1">{brandText}</Content>
        </MastheadBrand>
      </MastheadMain>
      <MastheadContent>
        <Toolbar isFullHeight>
          <ToolbarContent>
            {headerActions && <ToolbarItem>{headerActions}</ToolbarItem>}
            <ToolbarItem align={{ default: "alignEnd" }}>
              <Button
                variant="plain"
                onClick={toggleTheme}
                aria-label="Toggle theme"
              >
                {theme === "light" ? <MoonIcon /> : <SunIcon />}
              </Button>
            </ToolbarItem>
          </ToolbarContent>
        </Toolbar>
      </MastheadContent>
    </Masthead>
  );

  const sidebar = (
    <PageSidebar isSidebarOpen={sidebarOpen}>
      <PageSidebarBody>{nav}</PageSidebarBody>
    </PageSidebar>
  );

  return (
    <Page
      masthead={masthead}
      sidebar={sidebar}
      skipToContent={
        <SkipToContent href="#main-content">Skip to content</SkipToContent>
      }
      mainContainerId="main-content"
    >
      {children}
    </Page>
  );
}
